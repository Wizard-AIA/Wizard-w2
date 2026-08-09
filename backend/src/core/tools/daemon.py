"""The execution daemon and the client that talks to it.

There are two ways to run generated code -- in a Docker container, or in a local
subprocess -- and they are the *same* daemon over the same length-prefixed JSON
protocol. Only how the process is started, stopped and signalled differs.

Keeping one implementation matters more than it looks. The container path and
the Docker-less path had drifted before: the container preloaded every session
table into ``tables`` while the local path rebuilt a fresh namespace on each
call, so an investigation that computed something in one iteration found it gone
in the next. Anything that is true of one runtime is now true of both by
construction.

``DAEMON_SCRIPT`` is a string literal rather than a module because the container
receives it over ``put_archive`` into a stock Python image -- editing execution
semantics means editing that string. It is rendered with ``%``-formatting, so it
must contain no bare ``%`` characters.
"""

from __future__ import annotations

import json
import socket
import struct
from collections.abc import Callable

from src.core.tools.packages import DISTRIBUTION_NAMES, LIBS_DIRNAME


DAEMON_PATH = "/tmp/wizard_sandbox_daemon.py"
PID_FILE = "/tmp/wizard_sandbox_daemon.pid"
DAEMON_PORT = 5005

#: Top-level modules the daemon reports on when asked for its capabilities.
#:
#: The prompt used to describe the toolkit from a static tuple that had to be
#: kept in step with the Dockerfile by hand, and twice was not: scikit-learn and
#: statsmodels sat installed and unadvertised for months, and duckdb was
#: advertised into a process that did not have it. Asking the runtime what it
#: actually imported removes the coupling rather than documenting it.
PROBE_MODULES: tuple[str, ...] = (
    "pandas",
    "numpy",
    "pyarrow",
    "duckdb",
    "scipy",
    "statsmodels",
    "sklearn",
    "xgboost",
    "lightgbm",
    "lifelines",
    "networkx",
    "geopandas",
    "shapely",
    "matplotlib",
    "seaborn",
    "plotly",
    "openpyxl",
    "xlsxwriter",
    "tabulate",
    "PIL",
)


# Runs inside the container or the local subprocess. Kept as a string so it can
# be injected into a generic python image without rebuilding when it changes.
DAEMON_SCRIPT = '''
import base64
import builtins
import io
import json
import os
import socket
import struct
import subprocess
import sys
import traceback

PID_FILE = %(pid_file)r
ALLOW_PIP = %(allow_pip)s
WORKSPACE = %(workspace)r
BIND_HOST = %(bind_host)r
MEM_BYTES = %(mem_bytes)d
PROBE_MODULES = %(probe_modules)s
DISTRIBUTIONS = %(distributions)s
LIBS_DIR = os.path.join(WORKSPACE, %(libs_dirname)r)


def apply_memory_limit():
    """Caps address space so runaway code dies instead of swapping the host.

    POSIX only. Windows bounds a process through Job Objects, which needs
    pywin32; rather than pretend, the local runtime reports the cap as
    unenforced there.
    """
    if MEM_BYTES <= 0:
        return
    try:
        import resource
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = MEM_BYTES if hard in (resource.RLIM_INFINITY, -1) else min(MEM_BYTES, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    except (ValueError, OSError):
        pass


def recvall(sock, n):
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data


def read_message(sock):
    header = recvall(sock, 4)
    if not header:
        return None
    length = struct.unpack(">I", header)[0]
    payload = recvall(sock, length)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def send_message(sock, payload):
    raw = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw)


class StreamingStdout:
    """Mirrors writes to the client socket so the UI sees output live."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = io.StringIO()

    def write(self, text):
        self.buf.write(text)
        try:
            raw = json.dumps({"status": "stdout", "content": text}).encode("utf-8")
            self.sock.sendall(struct.pack(">I", len(raw)) + raw)
        except Exception:
            pass
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return self.buf.getvalue()


def probe_capabilities():
    """Module names that are importable here, without importing them.

    find_spec only locates the module, so probing twenty packages costs a path
    search rather than twenty imports -- which matters because this is asked on
    every prompt build.
    """
    from importlib.util import find_spec

    available = []
    for name in PROBE_MODULES:
        try:
            if find_spec(name) is not None:
                available.append(name)
        except (ImportError, ValueError, AttributeError):
            continue
    return available


def load_dataset(exec_globals, pd):
    """Binds `df` to the active table and `tables` to every loaded table.

    Cross-table questions are the common case for a real analytical request, so
    every table the session holds is in the namespace at once. `df` stays bound
    to the active one, which is what every existing prompt, cache entry and
    generated script already assumes.
    """
    tables = {}
    tables_dir = os.path.join(WORKSPACE, "tables")
    if os.path.isdir(tables_dir):
        for entry in sorted(os.listdir(tables_dir)):
            if not entry.endswith(".feather"):
                continue
            key = entry[: -len(".feather")]
            try:
                tables[key] = pd.read_feather(os.path.join(tables_dir, entry))
            except Exception as exc:
                print("Could not load table " + key + ": " + str(exc))
    exec_globals["tables"] = tables
    if tables:
        print("Tables available: " + ", ".join(sorted(tables)))

    for filename, reader in (
        ("dataset.feather", pd.read_feather),
        ("dataset.parquet", pd.read_parquet),
        ("dataset.csv", pd.read_csv),
    ):
        path = os.path.join(WORKSPACE, filename)
        if os.path.exists(path):
            try:
                exec_globals["df"] = reader(path)
                print("Dataset loaded from " + filename)
                return
            except Exception as exc:
                print("Could not load " + filename + ": " + str(exc))
    print("No dataset present yet.")


def install_missing(module_name):
    # The map is injected rather than duplicated, so the container path and the
    # parent-side installer cannot disagree about what `sklearn` is called.
    package = DISTRIBUTIONS.get(module_name, module_name)
    print("[sandbox] installing missing package: " + package)
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--quiet", "--disable-pip-version-check", package],
        timeout=300,
    )


def describe(value, pd):
    type_name = type(value).__name__
    shape = None
    preview = ""
    try:
        if isinstance(value, pd.DataFrame):
            shape = list(value.shape)
            preview = "Columns: " + str(list(value.columns[:8]))
        elif isinstance(value, pd.Series):
            shape = list(value.shape)
            preview = "Name: " + str(value.name) + ", dtype: " + str(value.dtype)
        elif hasattr(value, "shape"):
            shape = list(value.shape)
            preview = str(value)[:120]
        elif isinstance(value, (list, dict, set, tuple)):
            shape = len(value)
            preview = str(value)[:120]
        else:
            preview = str(value)[:120]
    except Exception:
        preview = "<unrepresentable>"
    return {"type": type_name, "shape": shape, "preview": preview}


def run_server(port=%(port)d):
    with open(PID_FILE, "w") as handle:
        handle.write(str(os.getpid()))

    # Libraries the parent installed for this session on demand. Prepended so a
    # session-local version wins, and present from the start so an install
    # between two executions needs no restart -- only a cache invalidation.
    if LIBS_DIR not in sys.path:
        sys.path.insert(0, LIBS_DIR)

    apply_memory_limit()

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    try:
        import seaborn as sns
    except Exception:
        sns = None

    exec_globals = {"pd": pd, "np": np, "plt": plt, "sns": sns, "__builtins__": __builtins__}
    load_dataset(exec_globals, pd)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND_HOST, port))
    server.listen(4)

    # The network policy is applied here and nowhere earlier: a filter that
    # refuses to create sockets cannot be installed before the listener exists.
    # accept() on an already-bound descriptor makes no socket() call, so the
    # connection the parent needs survives it.
    # `builtins` by name, never the `__builtins__` global: runpy runs this script
    # with that bound to a *dict*, so getattr would find nothing and the seal
    # would be silently skipped on every real session.
    sandbox_report = dict(getattr(builtins, "__wizard_sandbox__", None) or {})
    seal = getattr(builtins, "__wizard_seal__", None)
    if seal is not None:
        try:
            sandbox_report.update(seal() or {})
        except Exception as exc:
            sandbox_report["network"] = {"enforced": False, "detail": "seal failed: " + str(exc)}

    print("Sandbox daemon listening on port " + str(port))
    sys.stdout.flush()

    while True:
        conn = None
        try:
            conn, _ = server.accept()
            payload = read_message(conn)
            if not payload:
                conn.close()
                continue

            action = payload.get("action", "execute")

            if action == "ping":
                send_message(conn, {"status": "success", "pong": True})
                conn.close()
                continue

            if action == "capabilities":
                send_message(
                    conn,
                    {"status": "success", "modules": probe_capabilities(), "sandbox": sandbox_report},
                )
                conn.close()
                continue

            if action == "reload_dataset":
                load_dataset(exec_globals, pd)
                send_message(conn, {"status": "success"})
                conn.close()
                continue

            if action == "reset":
                exec_globals.clear()
                exec_globals.update(
                    {"pd": pd, "np": np, "plt": plt, "sns": sns, "__builtins__": __builtins__}
                )
                load_dataset(exec_globals, pd)
                send_message(conn, {"status": "success"})
                conn.close()
                continue

            if action == "inspect_variables":
                info = {}
                for name, value in list(exec_globals.items()):
                    if name.startswith("__"):
                        continue
                    if type(value).__name__ in ("module", "function", "builtin_function_or_method", "type"):
                        continue
                    info[name] = describe(value, pd)
                send_message(conn, {"status": "success", "variables": info})
                conn.close()
                continue

            code = payload.get("code", "")
            stdout_stream = StreamingStdout(conn)
            stderr_buffer = io.StringIO()
            real_stdout, real_stderr = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = stdout_stream, stderr_buffer

            plot_data = None
            status = "success"
            try:
                plt.close("all")
                # The parent may have installed into LIBS_DIR since the last
                # execution; without this the finder's negative cache still says
                # the module is missing.
                import importlib

                importlib.invalidate_caches()
                try:
                    exec(code, exec_globals)
                except ModuleNotFoundError as exc:
                    if not ALLOW_PIP:
                        raise
                    install_missing(exc.name)
                    exec(code, exec_globals)

                if plt.get_fignums():
                    buffer = io.BytesIO()
                    plt.savefig(buffer, format="png", bbox_inches="tight", dpi=110)
                    buffer.seek(0)
                    plot_data = base64.b64encode(buffer.read()).decode("utf-8")
                    plt.close("all")
            except KeyboardInterrupt:
                status = "interrupted"
                print("Execution interrupted.", file=stderr_buffer)
            except MemoryError:
                status = "error"
                stderr_buffer.write(
                    "MemoryError: this step exceeded the memory limit for the runtime.\\n"
                    "Work on a sample or in chunks, or raise SANDBOX_MEM_LIMIT."
                )
            except BaseException:
                status = "error"
                stderr_buffer.write(traceback.format_exc())
            finally:
                sys.stdout, sys.stderr = real_stdout, real_stderr

            send_message(
                conn,
                {
                    "status": status,
                    "stdout": "",
                    "stderr": stderr_buffer.getvalue(),
                    "plot": plot_data,
                },
            )
            conn.close()
        except Exception:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    run_server(int(sys.argv[1]) if len(sys.argv) > 1 else %(port)d)
'''


def render_daemon(
    *,
    port: int = DAEMON_PORT,
    pid_file: str = PID_FILE,
    allow_pip: bool = True,
    workspace: str = "/workspace",
    bind_host: str = "0.0.0.0",
    mem_bytes: int = 0,
) -> str:
    """Renders the daemon source for one runtime.

    Every interpolated string -- ``workspace``, ``pid_file`` and ``bind_host``
    -- is written with ``%r``, i.e. as a Python ``repr``, so the interpreter's
    own escaping produces the literal. ``bind_host`` used to be spliced into a
    ``"..."`` literal with plain ``%s``: every current caller passes a fixed
    constant, but a value containing a quote, backslash or newline would have
    broken out of the string and run as code inside the sandbox daemon before
    any policy applied. ``%r`` closes that off structurally rather than by
    trusting every future caller to keep passing a safe literal.

    ``workspace`` is a real host path on the local backend and ``/workspace``
    inside a container. Both paths are written with ``%r`` for the same reason.

    Interpolating them into ``"..."`` and relying on ``Path.as_posix()`` to
    remove the backslashes did not work, because ``as_posix()`` is only a
    conversion on Windows: on Linux and macOS a Windows-style path is one
    opaque filename, the backslashes survive, and ``C:\\Users\\...`` becomes an
    invalid ``\\U`` escape that makes the whole daemon unparseable. ``repr``
    is correct on every platform and preserves the native separators, which is
    what the daemon actually wants -- it runs on the same OS as this process.
    """
    return DAEMON_SCRIPT % {
        "port": port,
        "pid_file": str(pid_file),
        "allow_pip": "True" if allow_pip else "False",
        "workspace": str(workspace),
        "bind_host": bind_host,
        "mem_bytes": int(mem_bytes),
        "probe_modules": json.dumps(list(PROBE_MODULES)),
        "distributions": json.dumps(DISTRIBUTION_NAMES),
        "libs_dirname": LIBS_DIRNAME,
    }


class DaemonUnavailableError(RuntimeError):
    """Raised when a runtime is required but cannot be provided."""


def find_free_port() -> int:
    # Loopback, matching the daemon protocol itself -- this only probes the OS
    # for an unused port number, but binding "" (all interfaces) here would be
    # inconsistent with the "loopback only" model even though the socket is
    # closed again immediately.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DaemonClient:
    """Socket protocol shared by every execution runtime.

    Subclasses own the process lifecycle -- ``start``, ``stop``, ``interrupt`` --
    and expose where to reach it through :meth:`endpoint`.
    """

    #: Set by the subclass once the process is listening.
    port: int | None = None
    session_id: str = ""

    # ------------------------------------------------------------------ #
    def endpoint(self) -> tuple[str, int]:
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        raise NotImplementedError

    def interrupt(self) -> bool:
        """Signals the running cell. Overridden per backend."""
        return False

    def stop(self) -> None:
        """Tears the runtime down. Overridden per backend."""

    # ------------------------------------------------------------------ #
    def _request(self, payload: dict, on_stdout: Callable[[str], None] | None = None) -> dict:
        """Sends one request and drains the reply, forwarding stdout frames."""
        from src.config import settings

        if not self.is_running:
            raise DaemonUnavailableError("Execution runtime is not running.")

        timeout = settings.SANDBOX_EXEC_TIMEOUT
        host, port = self.endpoint()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            raw = json.dumps(payload).encode("utf-8")
            sock.sendall(struct.pack(">I", len(raw)) + raw)

            stdout_parts: list[str] = []
            while True:
                header = self._recv_exactly(sock, 4)
                if header is None:
                    raise DaemonUnavailableError("Runtime closed the connection unexpectedly.")
                length = struct.unpack(">I", header)[0]
                body = self._recv_exactly(sock, length)
                if body is None:
                    raise DaemonUnavailableError("Truncated response from the runtime.")

                message = json.loads(body.decode("utf-8"))
                if message.get("status") == "stdout":
                    chunk = message.get("content", "")
                    stdout_parts.append(chunk)
                    if on_stdout and chunk.strip():
                        on_stdout(chunk)
                    continue

                message["stdout"] = "".join(stdout_parts) + (message.get("stdout") or "")
                return message
        finally:
            sock.close()

    @staticmethod
    def _recv_exactly(sock: socket.socket, count: int) -> bytearray | None:
        data = bytearray()
        while len(data) < count:
            packet = sock.recv(count - len(data))
            if not packet:
                return None
            data.extend(packet)
        return data

    # ------------------------------------------------------------------ #
    def run_code(self, code: str, on_stdout: Callable[[str], None] | None = None) -> tuple[str, str | None]:
        """Executes ``code``. Returns ``(output_text, base64_png_or_None)``."""
        from src.config import settings
        from src.utils.logging import logger

        lock = getattr(self, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    response = self._request({"action": "execute", "code": code}, on_stdout)
            else:  # pragma: no cover - every concrete runtime carries a lock
                response = self._request({"action": "execute", "code": code}, on_stdout)
        except TimeoutError:
            return (
                f"Error executing code:\nExecution exceeded the {settings.SANDBOX_EXEC_TIMEOUT}s time limit.",
                None,
            )
        except DaemonUnavailableError as exc:
            return f"Error executing code:\n{exc}", None
        except Exception as exc:
            logger.error("Runtime communication failed", error=str(exc))
            return f"Error executing code:\nRuntime communication failure: {exc}", None

        status = response.get("status")
        stdout = (response.get("stdout") or "").strip()
        stderr = (response.get("stderr") or "").strip()

        if status == "interrupted":
            return "Execution interrupted by user.", None
        if status == "error":
            detail = stderr or "Unknown execution error."
            return f"Error executing code:\n{detail}", None
        return (stdout or "Executed successfully."), response.get("plot")

    def _simple(self, action: str, key: str | None = None, default=None):
        """One request/response with no streaming, swallowing transport errors."""
        from src.utils.logging import logger

        lock = getattr(self, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    response = self._request({"action": action})
            else:  # pragma: no cover
                response = self._request({"action": action})
        except Exception as exc:
            logger.warning("Runtime action failed", action=action, error=str(exc))
            return default
        return response if key is None else response.get(key, default)

    def inspect_variables(self) -> dict:
        return self._simple("inspect_variables", "variables", {}) or {}

    def capabilities(self) -> frozenset[str]:
        """Modules importable in the runtime, as reported by the runtime."""
        modules = self._simple("capabilities", "modules", None)
        return frozenset(modules) if modules else frozenset()

    def sandbox_report(self) -> dict:
        """What the runtime says was **actually** applied to it.

        Asked of the child rather than derived from the configuration, because
        the milestone's claim is enforced containment and only the process that
        made the syscalls knows whether they succeeded.
        """
        return self._simple("capabilities", "sandbox", {}) or {}

    def reload_dataset(self) -> bool:
        """Re-reads the datasets from the workspace without restarting."""
        return self._simple("reload_dataset") is not None

    def reset_namespace(self) -> bool:
        return self._simple("reset") is not None

    def ping(self) -> bool:
        return self._simple("ping", "pong", False) is True


__all__ = [
    "DAEMON_PATH",
    "DAEMON_PORT",
    "DAEMON_SCRIPT",
    "PID_FILE",
    "PROBE_MODULES",
    "DaemonClient",
    "DaemonUnavailableError",
    "find_free_port",
    "render_daemon",
]
