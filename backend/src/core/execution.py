"""The single path through which generated code reaches a Python interpreter.

Previously there were three: the sandbox, an in-process ``exec`` fallback in the
agent, and a *completely unguarded* ``exec`` in ``ScientificAgent.clean_dataset``
that ran on every upload. Only the first was checked. Everything now funnels
through :meth:`CodeExecutor.execute`, which guards first and executes second.

Which runtime serves a session is decided by :mod:`src.core.tools.runtime`: a
host subprocess (the default), a container, or -- only when spawning is forbidden
-- a guarded ``exec`` in this process. The first two are both fully supported;
the third is the genuinely degraded one and says so.
"""

from __future__ import annotations

import base64
import builtins
import io
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.config import settings
from src.core.security.code_guard import CodeGuard, GuardVerdict
from src.core.tools import runtime as runtime_backend
from src.utils.logging import logger


# Builtins removed from the in-process fallback namespace. A mitigation, not a
# boundary. Anything the guard already rejects will never get here.
BLOCKED_BUILTINS = frozenset(
    {"eval", "exec", "compile", "open", "input", "exit", "quit", "help", "__import__", "breakpoint"}
)


@dataclass
class ExecutionResult:
    output: str
    code: str
    image: str | None = None
    ok: bool = True
    blocked: bool = False
    blocked_reason: str = ""
    #: Populated only when *every* violation was a path outside the writable
    #: roots. That is the one kind of block a user can legitimately lift, so the
    #: paths travel structurally rather than being read back out of the sentence.
    blocked_paths: list[str] = field(default_factory=list)
    retryable_error: bool = False
    #: True when a real boundary was in force. Derived from :attr:`isolation`
    #: rather than set directly -- it used to mean "container specifically",
    #: which stopped being the same question once the host backend could be
    #: contained by the OS.
    sandboxed: bool = True
    #: Which backend actually ran this: ``host`` / ``docker`` / ``inprocess``.
    backend: str = "docker"
    #: What was actually containing the code: ``container``, ``os-sandbox``,
    #: ``process`` (a separate process, no OS policy applied) or ``none``.
    isolation: str = "container"
    warnings: list[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "ok": self.ok,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "sandboxed": self.sandboxed,
            "backend": self.backend,
            "isolation": self.isolation,
            "has_image": self.image is not None,
            "warnings": self.warnings,
        }


#: How much containment each backend carries. ``host`` becomes ``os-sandbox``
#: once the OS policy layer lands; until then it is honestly just a process.
ISOLATION_BY_BACKEND = {"docker": "container", "host": "process", "inprocess": "none"}


def isolation_for(backend: str) -> str:
    return ISOLATION_BY_BACKEND.get(backend, "none")


_local_exec_lock = threading.Lock()


class CodeExecutor:
    """Guards, repairs and runs generated Python for a given session."""

    def __init__(self, session_id: str):
        self.session_id = session_id

    # ------------------------------------------------------------------ #
    def guard(self, code: str, allowed_roots: tuple[str, ...] = ()) -> tuple[GuardVerdict, str]:
        """Repairs then scans. Returns the verdict and the (possibly repaired) code.

        On a non-container backend the writable root is the session's own
        workspace directory rather than ``/workspace``, so it is passed in --
        otherwise the guard would reject the very chart path the prompt handed
        the model.

        ``allowed_roots`` carries directories the user has explicitly consented
        to. The guard is not weakened by this; it is the mechanism by which it
        can be told yes, which it previously had no way to be.

        The session's own workspace is always passed **first**, because the guard
        resolves a *relative* path against the first root. Appending a consented
        directory ahead of it would quietly redefine what `to_csv("out.csv")`
        means -- consent to write to one directory is not a request to move the
        working directory there.
        """
        _, repaired = CodeGuard.repair(code)
        backend = runtime_backend.active_backend()
        workspace = "/workspace" if backend == "docker" else runtime_backend.workspace_for(self.session_id).as_posix()
        return CodeGuard.scan(repaired, extra_roots=(workspace, *allowed_roots)), repaired

    def execute(
        self,
        code: str,
        df: pd.DataFrame | None = None,
        on_stdout: Callable[[str], None] | None = None,
        tables: dict[str, pd.DataFrame] | None = None,
        allowed_roots: tuple[str, ...] = (),
    ) -> ExecutionResult:
        """Runs ``code`` on whichever runtime serves this session.

        ``tables`` matters only on the in-process path: both daemons read every
        session table off the workspace at startup, so passing them again would
        pay a full serialisation per call for something already there.
        """
        verdict, prepared = self.guard(code, allowed_roots)
        backend = runtime_backend.active_backend()

        if not verdict.ok:
            if verdict.syntax_error:
                # Malformed output is the model's problem to fix; route it back
                # into the correction loop rather than reporting a policy block.
                return ExecutionResult(
                    output=f"Error executing code:\n{verdict.reason}",
                    code=prepared,
                    ok=False,
                    retryable_error=True,
                    sandboxed=isolation_for(backend) in ("container", "os-sandbox"),
                    backend=backend,
                    isolation=isolation_for(backend),
                )
            logger.warning("Execution blocked by guard", reason=verdict.reason, session=self.session_id)
            return ExecutionResult(
                output=f"This step was blocked by the safety guard: {verdict.reason}",
                code=prepared,
                ok=False,
                blocked=True,
                blocked_reason=verdict.reason,
                blocked_paths=list(verdict.paths) if verdict.only_paths else [],
                sandboxed=isolation_for(backend) in ("container", "os-sandbox"),
                backend=backend,
                isolation=isolation_for(backend),
            )

        runtime = runtime_backend.get_runtime(self.session_id)
        if runtime is not None:
            output, image = runtime.run_code(prepared, on_stdout)
            failed = output.startswith("Error executing code:")
            return ExecutionResult(
                output=output,
                code=prepared,
                image=image,
                ok=not failed,
                retryable_error=failed,
                sandboxed=isolation_for(backend) in ("container", "os-sandbox"),
                backend=backend,
                isolation=isolation_for(backend),
                # No warning for `host`: it is a supported way to run, and the
                # isolation actually in force is reported once on /settings
                # rather than restated on every message. Only the in-process
                # path below warns, because only it has no isolation at all.
            )

        return self._execute_locally(prepared, df, on_stdout, tables)

    # ------------------------------------------------------------------ #
    def _execute_locally(
        self,
        code: str,
        df: pd.DataFrame | None,
        on_stdout: Callable[[str], None] | None = None,
        tables: dict[str, pd.DataFrame] | None = None,
    ) -> ExecutionResult:
        """Last resort: guarded ``exec`` in the API process itself.

        Reached only when neither a container nor a subprocess can be had. The
        namespace does not persist between calls and nothing bounds the code, so
        this is the one path that genuinely warrants a warning.
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        try:
            import polars as pl
        except ImportError:
            pl = None

        try:
            import seaborn as sns
        except ImportError:
            sns = None

        from src.core.tools.stats import StatisticalToolkit

        safe_builtins = {name: value for name, value in vars(builtins).items() if name not in BLOCKED_BUILTINS}
        namespace: dict[str, Any] = {
            "pd": pd,
            "np": np,
            "pl": pl,
            "plt": plt,
            "sns": sns,
            "stats": StatisticalToolkit,
            # Always present, even when empty, so generated code can reference
            # `tables` unconditionally rather than guarding every use.
            "tables": {name: frame.copy() for name, frame in (tables or {}).items()},
            "__builtins__": safe_builtins,
        }
        if df is not None:
            namespace["df"] = df.copy()

        buffer = io.StringIO()
        original_stdout = sys.stdout
        warning = (
            "No isolated runtime was available, so this ran in a restricted interpreter "
            "inside the API process. Set EXECUTION_BACKEND=host in backend/.env to run it in a "
            "separate process instead."
        )

        with _local_exec_lock:
            try:
                plt.close("all")
                sys.stdout = buffer
                exec(code, namespace)  # noqa: S102 - guarded above; Docker is the real boundary
                sys.stdout = original_stdout

                image = None
                if plt.get_fignums():
                    image_buffer = io.BytesIO()
                    plt.savefig(image_buffer, format="png", bbox_inches="tight", dpi=110)
                    image_buffer.seek(0)
                    image = base64.b64encode(image_buffer.read()).decode("utf-8")
                    plt.close("all")

                output = buffer.getvalue().strip() or "Executed successfully."
                if on_stdout and output:
                    on_stdout(output)
                return ExecutionResult(
                    output=output,
                    code=code,
                    image=image,
                    ok=True,
                    sandboxed=False,
                    backend="inprocess",
                    isolation="none",
                    warnings=[warning],
                )
            except Exception as exc:
                import traceback

                sys.stdout = original_stdout
                detail = traceback.format_exc(limit=6)
                logger.warning("Local execution failed", error=str(exc))
                return ExecutionResult(
                    output=f"Error executing code:\n{detail}",
                    code=code,
                    ok=False,
                    retryable_error=True,
                    sandboxed=False,
                    backend="inprocess",
                    isolation="none",
                    warnings=[warning],
                )
            finally:
                sys.stdout = original_stdout
                plt.close("all")

    # ------------------------------------------------------------------ #
    # Runtime control. All of these address an *existing* runtime only --
    # ``create=False`` -- because inspecting or resetting a session that has not
    # run anything should not be what brings a container up.
    # ------------------------------------------------------------------ #
    def _existing(self):
        return runtime_backend.get_runtime(self.session_id, create=False)

    @property
    def backend(self) -> str:
        return runtime_backend.active_backend()

    def inspect_variables(self) -> dict:
        runtime = self._existing()
        return runtime.inspect_variables() if runtime else {}

    def interrupt(self) -> bool:
        runtime = self._existing()
        return runtime.interrupt() if runtime else False

    def reload_dataset(self) -> bool:
        runtime = self._existing()
        return runtime.reload_dataset() if runtime else False

    def reset(self) -> bool:
        runtime = self._existing()
        return runtime.reset_namespace() if runtime else False

    def capabilities(self) -> frozenset[str]:
        """Modules generated code may import in this session's runtime."""
        return runtime_backend.capabilities(self.session_id)


def plot_output_path(session_id: str) -> str:
    """Path the worker is told to write interactive charts to.

    ``/workspace`` inside a container; the session's own directory for a host
    runtime, which is started with that directory as its working directory and
    as the daemon's ``WORKSPACE``.
    """
    return runtime_backend.workspace_path(session_id, "plot.html")


def host_plot_path(session_id: str):
    """Corresponding path on the host for the session's chart."""
    return runtime_backend.workspace_for(session_id) / "plot.html"


__all__ = ["CodeExecutor", "ExecutionResult", "isolation_for", "plot_output_path", "host_plot_path", "settings"]
