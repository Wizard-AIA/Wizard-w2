"""Proof that the sandbox does what it claims, by trying to break it.

The milestone's acceptance criterion is *verifiable* containment rather than
documented containment, and a configuration flag is not evidence. This spawns a
short-lived child through the real machinery -- the same policy, the same
bootstrap, the same ``plan_spawn`` -- and has it attempt each forbidden
operation, reporting what the kernel actually did.

It is the only thing in the codebase that runs the sandbox for real. Everything
else asserts on inert data, because the suite may not spawn a process; this is
reached from ``GET /api/sandbox/selftest`` and from the opt-in tests under
``backend/tests/sandbox/``.

Written to be honest in three directions rather than two: a check reports
``blocked``, ``allowed``, or ``inconclusive`` -- the last for the case where
nothing stopped the attempt but nothing proves it would have succeeded either.
Calling that a pass would be exactly the kind of claim this module exists to
stop.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from src.core.security.sandbox.bootstrap import render_bootstrap
from src.core.security.sandbox.capability import detect
from src.core.security.sandbox.policy import SandboxPolicy, policy_for
from src.core.security.sandbox.spawn import SandboxUnavailableError, plan_spawn
from src.utils.logging import logger


#: Runs inside the sandboxed child. Deliberately imports nothing heavy: the
#: point is to measure the boundary, not to pay for pandas while doing it.
#:
#: `198.51.100.1` is TEST-NET-2 (RFC 5737) and is guaranteed not to route, so a
#: machine with an open sandbox never actually reaches anything -- an unblocked
#: attempt times out instead of contacting a real host.
PROBE_SCRIPT = """
import builtins
import json
import os
import socket
import sys

WORKSPACE = %(workspace)r
OUTSIDE = %(outside)r
MEM_BYTES = %(mem_bytes)d

results = {}


def record(name, outcome, detail):
    results[name] = {"outcome": outcome, "detail": detail}


try:
    seal = getattr(builtins, "__wizard_seal__", None)
    if seal is not None:
        seal()
except Exception as exc:
    record("seal", "inconclusive", str(exc))

try:
    target = os.path.join(WORKSPACE, "selftest.txt")
    with open(target, "w") as handle:
        handle.write("ok")
    os.unlink(target)
    record("workspace_write", "allowed", "wrote and removed a file in the workspace")
except Exception as exc:
    # The sandbox denying its own workspace is a misconfiguration, not a pass:
    # the runtime would be unable to write a chart or a CSV export.
    record("workspace_write", "blocked", "the workspace is NOT writable: " + str(exc))

try:
    with open(OUTSIDE, "w") as handle:
        handle.write("escaped")
    try:
        os.unlink(OUTSIDE)
    except Exception:
        pass
    record("outside_write", "allowed", "wrote to " + OUTSIDE)
except Exception as exc:
    record("outside_write", "blocked", type(exc).__name__ + ": " + str(exc))

try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(("198.51.100.1", 80))
        record("outbound_network", "allowed", "connected to a TEST-NET address")
    except socket.timeout:
        # Nothing refused it, but TEST-NET does not route, so this is not proof
        # that a reachable host could have been contacted.
        record("outbound_network", "inconclusive", "no refusal, but the attempt timed out")
    except OSError as exc:
        record("outbound_network", "blocked", type(exc).__name__ + ": " + str(exc))
    finally:
        sock.close()
except OSError as exc:
    record("outbound_network", "blocked", "socket() refused: " + str(exc))

if MEM_BYTES > 0:
    try:
        chunk = bytearray(MEM_BYTES + (256 * 1024 * 1024))
        record("memory_ceiling", "allowed", "allocated past the ceiling (" + str(len(chunk)) + " bytes)")
        del chunk
    except MemoryError:
        record("memory_ceiling", "blocked", "MemoryError at the ceiling")
    except Exception as exc:
        record("memory_ceiling", "blocked", type(exc).__name__ + ": " + str(exc))
else:
    record("memory_ceiling", "inconclusive", "no ceiling configured")

sandbox = getattr(builtins, "__wizard_sandbox__", None)
print("WIZARD_SELFTEST " + json.dumps({"checks": results, "applied": sandbox}))
sys.stdout.flush()
"""


@dataclass
class SelfTestResult:
    ok: bool
    detail: str
    checks: dict
    applied: dict

    def to_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "checks": self.checks, "applied": self.applied}


def _render_probe(policy: SandboxPolicy, workspace: Path) -> str:
    # Somewhere outside every writable root but plainly writable without a
    # sandbox: the user's home is the directory the whole exercise is about.
    outside = Path.home() / ".wizard-sandbox-selftest"
    return PROBE_SCRIPT % {
        "workspace": str(workspace),
        "outside": str(outside),
        "mem_bytes": policy.mem_bytes,
    }


def run(timeout: float = 60.0) -> SelfTestResult:
    """Spawns one probe child and reports what it was actually prevented from doing.

    Never raises: this is a diagnostics endpoint, and a self-test that 500s tells
    the user less than one that says it could not run.
    """
    capability = detect()

    # A plain directory under WORKSPACE_DIR, not `tempfile.TemporaryDirectory`:
    # `mkdtemp` documents its result as "readable, writable, and searchable
    # only by the creating user" and gets there on Windows by giving the
    # directory a fresh, non-inherited ACL -- so it grants the current user
    # Full Control even on a host where the real workspace root does not. A
    # real session's workspace is created with plain `Path.mkdir()`
    # (`resolve_workspace_dir` in `tools/runtime.py`), which inherits
    # whatever ACL the parent already has. Reusing that exact call is what
    # makes this probe fail the same way a real session's `icacls
    # /setintegritylevel` does when the workspace root sits somewhere the
    # user doesn't have WRITE_DAC on -- `tempfile` would silently paper over
    # that and report containment that no real session actually gets.
    from src.config import settings

    directory = settings.WORKSPACE_DIR / f"wizard-selftest-{uuid.uuid4().hex[:12]}"
    directory.mkdir(parents=True)
    try:
        workspace = directory
        probe_path = workspace / "probe.py"
        entry = probe_path

        try:
            policy = policy_for(workspace)
            probe_path.write_text(_render_probe(policy, workspace), encoding="utf-8")

            if policy.enabled:
                entry = workspace / "bootstrap.py"

            # `plan_spawn` attempts the Windows workspace label as a side
            # effect, so it has to run before the bootstrap is rendered --
            # see the matching comment in `tools/host_runtime.py`.
            plan = plan_spawn(policy, [sys.executable, "-u", str(entry)], workspace)

            if policy.enabled:
                bootstrap_policy = policy
                if sys.platform == "win32" and "low-integrity" not in plan.mechanism:
                    bootstrap_policy = replace(policy, windows_lower_integrity=False)
                entry.write_text(render_bootstrap(bootstrap_policy, probe_path), encoding="utf-8")
        except SandboxUnavailableError as exc:
            return SelfTestResult(False, str(exc), {}, {})
        except OSError as exc:
            return SelfTestResult(False, f"Could not prepare the probe: {exc}", {}, {})

        environment = dict(plan.env)
        try:
            completed = subprocess.run(  # noqa: S603 - argv we built, no shell
                plan.argv,
                cwd=str(workspace),
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**_base_environment(), **environment},
            )
        except subprocess.TimeoutExpired:
            return SelfTestResult(False, f"The probe did not finish within {timeout:.0f}s", {}, {})
        except OSError as exc:
            return SelfTestResult(False, f"Could not run the probe: {exc}", {}, {})
        finally:
            if plan.teardown is not None:
                plan.teardown()

        stdout = (completed.stdout or b"").decode("utf-8", "replace")
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()

        line = next((ln for ln in stdout.splitlines() if ln.startswith("WIZARD_SELFTEST ")), "")
        if not line:
            detail = f"the probe produced no report (exit code {completed.returncode}, stderr={stderr[:300]!r}, stdout={stdout[:300]!r})"
            logger.warning("Sandbox self-test produced no report", detail=detail)
            return SelfTestResult(False, detail, {}, {})

        try:
            payload = json.loads(line[len("WIZARD_SELFTEST ") :])
        except json.JSONDecodeError as exc:
            return SelfTestResult(False, f"Unreadable report: {exc}", {}, {})
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    checks = payload.get("checks") or {}
    applied = payload.get("applied") or {}
    return SelfTestResult(*_judge(checks, capability), checks, applied)


def _base_environment() -> dict[str, str]:
    """A minimal environment. The probe needs an interpreter and nothing else."""
    import os

    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG", "LD_LIBRARY_PATH")
    return {name: os.environ[name] for name in keep if name in os.environ}


def _judge(checks: dict, capability) -> tuple[bool, str]:
    """Turns the raw outcomes into a verdict, per what this OS claims to support.

    A feature the platform says it cannot enforce is not counted as a failure --
    that gap is already reported by name, and failing the whole self-test for it
    would train the reader to ignore a red result on the machines that have one.
    """
    supported = {feature.key: feature.supported for feature in capability.features}
    failures = []

    if checks.get("workspace_write", {}).get("outcome") != "allowed":
        failures.append("the workspace is not writable")

    if supported.get("filesystem") and checks.get("outside_write", {}).get("outcome") != "blocked":
        failures.append("a write outside the workspace was not blocked")

    if supported.get("network") and checks.get("outbound_network", {}).get("outcome") == "allowed":
        failures.append("an outbound connection was not blocked")

    if failures:
        return False, "; ".join(failures)
    return True, "every restriction this platform supports was enforced"


__all__ = ["PROBE_SCRIPT", "SelfTestResult", "run"]
