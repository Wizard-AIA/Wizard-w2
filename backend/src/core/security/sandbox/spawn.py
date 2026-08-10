"""How a sandboxed child is launched, per platform.

Three shapes, because the OSes disagree about when a restriction can be applied:

* **Linux** -- nothing here. Landlock and seccomp are applied by the child to
  itself, which is the only way to bound a process that is already running the
  interpreter it needs.
* **macOS** -- the restriction *is* the command line: ``sandbox-exec -f profile``
  wraps the interpreter, so it has to be decided before the process exists.
* **Windows** -- a job object exists before the child and adopts it immediately
  after; the integrity level is dropped by the child, since a process may lower
  its own.

:class:`SpawnPlan` is what comes back: an argv, extra environment, and a
teardown hook. Returning a plan rather than spawning keeps
:class:`~src.core.tools.host_runtime.HostSession` the single owner of process
lifecycle -- it already handles the pipes, the process group and the interrupt,
and none of that should be duplicated per platform.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.core.security.sandbox.capability import detect
from src.core.security.sandbox.policy import SandboxPolicy
from src.core.security.sandbox.profiles import sbpl_profile
from src.utils.logging import logger


class SandboxUnavailableError(RuntimeError):
    """Raised under ``HOST_SANDBOX=require`` when the policy cannot be applied."""


@dataclass
class SpawnPlan:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    #: Called after the child is running, to adopt it into a job object.
    adopt: Callable[[object], None] | None = None
    #: Called when the runtime stops, to release whatever ``adopt`` needed.
    teardown: Callable[[], None] | None = None
    #: What this plan claims to enforce, for logging and the settings surface.
    mechanism: str = "none"


def _macos_plan(policy: SandboxPolicy, argv: list[str], workspace: Path) -> SpawnPlan:
    profile_path = workspace / ".runtime_sandbox.sb"
    profile_path.write_text(sbpl_profile(policy), encoding="utf-8")
    return SpawnPlan(
        argv=["/usr/bin/sandbox-exec", "-f", str(profile_path), *argv],
        mechanism="sandbox-exec",
    )


def _windows_plan(policy: SandboxPolicy, argv: list[str], workspace: Path) -> SpawnPlan:
    from src.core.security.sandbox import windows

    labeled = windows.label_workspace_low(workspace)
    job = windows.create_job(policy.mem_bytes, policy.max_processes)

    # The job object bounds memory/process count either way; integrity
    # lowering is only safe to claim -- and only worth the child attempting --
    # when the workspace label actually took. A caller threads this back into
    # the policy the child receives; see `windows_lower_integrity`.
    mechanism = "job-object+low-integrity" if labeled else "job-object"
    return SpawnPlan(
        argv=argv,
        adopt=lambda process: windows.assign_to_job(job, process),  # type: ignore[arg-type]
        teardown=lambda: windows.close_job(job),
        mechanism=mechanism,
    )


def plan_spawn(policy: SandboxPolicy, argv: list[str], workspace: Path) -> SpawnPlan:
    """Decorates ``argv`` with whatever this platform applies before the child runs.

    Under ``best-effort`` an unavailable mechanism yields a plain spawn and a
    warning; under ``require`` it raises, because someone who set ``require``
    asked for a boundary and a subprocess that merely looks like one is the
    wrong answer to give them silently.
    """
    if not policy.enabled:
        return SpawnPlan(argv=argv, mechanism="none")

    capability = detect()
    if not capability.any_supported:
        message = f"No OS sandbox is available here: {capability.features[0].detail}"
        if policy.required:
            raise SandboxUnavailableError(message)
        logger.warning("Host sandbox unavailable; running without OS containment", detail=message)
        return SpawnPlan(argv=argv, env=policy.cache_environment(), mechanism="none")

    try:
        if sys.platform == "darwin":
            if capability.features[0].supported:
                plan = _macos_plan(policy, argv, workspace)
            else:
                plan = SpawnPlan(argv=argv, mechanism="none")
        elif sys.platform == "win32":
            plan = _windows_plan(policy, argv, workspace)
        else:
            plan = SpawnPlan(argv=argv, mechanism=capability.mechanism)
    except OSError as exc:
        if policy.required:
            raise SandboxUnavailableError(f"Could not prepare the sandbox: {exc}") from exc
        logger.warning("Could not prepare the host sandbox", error=str(exc))
        plan = SpawnPlan(argv=argv, mechanism="none")

    plan.env.update(policy.cache_environment())
    return plan


__all__ = ["SandboxUnavailableError", "SpawnPlan", "plan_spawn"]
