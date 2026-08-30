"""Docker execution backend: one container per session.

Changes from the previous implementation
----------------------------------------
* **Lazy, not import-time.** ``SandboxManager()`` used to build an image and start
  a container as a side effect of importing ``src.api.api``. That made every test
  run and every CI job depend on a Docker daemon. Containers are now created on
  first use and only when Docker is both enabled and reachable.
* **Per-session containers.** One shared container meant one shared ``exec_globals``
  namespace: any user could read another user's variables. Each session now gets
  its own container and its own bind-mounted workspace subdirectory.
* **Interrupt actually interrupts.** ``interrupt()`` sent ``kill -2 1``, but PID 1
  is the container's ``sleep`` command, not the daemon -- pressing Stop destroyed
  the sandbox. The daemon now records its own PID and is signalled directly.
* **No redundant serialisation.** ``run_code`` accepted a Feather-encoded frame it
  then ignored, so every execution paid a full DataFrame serialisation for
  nothing. The dataset is passed through the bind mount instead.
* **Resource limits.** Memory, PID and CPU caps plus ``no-new-privileges`` and
  dropped capabilities, so runaway generated code cannot exhaust the host.
* **Execution timeout.** A socket deadline bounds any single execution.

The wire protocol and the daemon source itself live in :mod:`daemon`, because the
host subprocess backend runs exactly the same daemon -- see
:mod:`src.core.tools.host_runtime`.
"""

from __future__ import annotations

import atexit
import io
import os
import socket
import tarfile
import threading
import time
from pathlib import Path

from src.config import settings
from src.core.tools.daemon import (
    DAEMON_PATH,
    DAEMON_PORT,
    DAEMON_SCRIPT,
    PID_FILE,
    DaemonClient,
    DaemonUnavailableError,
    find_free_port,
    render_daemon,
)
from src.utils.logging import logger


#: Kept as an alias rather than a distinct type: callers catch "the runtime
#: could not serve this", which is one condition however it is reached.
SandboxUnavailableError = DaemonUnavailableError


def _in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _connect_host() -> str:
    """Address the backend uses to reach a published sandbox port."""
    return "host.docker.internal" if _in_container() else "127.0.0.1"


def _bind_host() -> str:
    """Interface the sandbox port is published on."""
    return "0.0.0.0" if _in_container() else "127.0.0.1"


class SandboxSession(DaemonClient):
    """One container bound to one user session."""

    def __init__(self, client, session_id: str, workspace_dir: Path):
        self.client = client
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self.container = None
        self.port: int | None = None
        self.created_at = time.time()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    @property
    def image_name(self) -> str:
        return settings.sandbox_image

    @property
    def is_running(self) -> bool:
        return self.container is not None

    def endpoint(self) -> tuple[str, int]:
        return _connect_host(), int(self.port or DAEMON_PORT)

    def start(self):
        """Creates the container and waits for the daemon to accept connections."""
        if self.container is not None:
            return

        import docker.errors

        self.port = find_free_port()
        host_workspace = self._resolve_host_workspace()

        run_kwargs: dict = {
            "image": self.image_name,
            "command": "sleep infinity",
            "ports": {f"{DAEMON_PORT}/tcp": (_bind_host(), self.port)},
            "volumes": {host_workspace: {"bind": "/workspace", "mode": "rw"}},
            "working_dir": "/workspace",
            "detach": True,
            "labels": {"wizard_managed": "true", "wizard_session": self.session_id},
            "network_disabled": settings.SANDBOX_NETWORK_DISABLED,
            # Containment: generated code must not be able to starve the host or
            # gain privileges beyond what it starts with.
            "mem_limit": settings.SANDBOX_MEM_LIMIT,
            "pids_limit": settings.SANDBOX_PIDS_LIMIT,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
        }
        if settings.SANDBOX_CPU_QUOTA > 0:
            run_kwargs["cpu_quota"] = settings.SANDBOX_CPU_QUOTA
            run_kwargs["cpu_period"] = 100_000
        if settings.SANDBOX_DOCKER_RUNTIME:
            run_kwargs["runtime"] = settings.SANDBOX_DOCKER_RUNTIME

        try:
            self.container = self.client.containers.run(**run_kwargs)
        except docker.errors.ImageNotFound:
            self._build_image()
            self.container = self.client.containers.run(**run_kwargs)

        # The container's memory ceiling is enforced by Docker, so the daemon's
        # own RLIMIT would only duplicate it -- and a soft limit inside a
        # hard-limited cgroup turns an OOM kill into a confusing MemoryError.
        script = render_daemon(
            port=DAEMON_PORT,
            pid_file=PID_FILE,
            allow_pip=settings.SANDBOX_ALLOW_RUNTIME_PIP,
            workspace="/workspace",
            bind_host="0.0.0.0",
            mem_bytes=0,
        )
        self._put_file(DAEMON_PATH, script)
        self.container.exec_run(f"python {DAEMON_PATH}", detach=True)

        if not self._wait_ready():
            logger.warning("Sandbox daemon did not become ready", session=self.session_id)

        logger.info("Sandbox session started", session=self.session_id, port=self.port)

    def _build_image(self):
        docker_context = Path(__file__).resolve().parents[3] / "docker"
        logger.info(
            "Building sandbox image",
            image=self.image_name,
            tier=settings.SANDBOX_TIER,
            context=str(docker_context),
        )
        self.client.images.build(
            path=str(docker_context),
            tag=self.image_name,
            rm=True,
            # The tier decides which library layers are installed. It is part of
            # the tag too, so switching tiers builds a new image instead of
            # silently reusing one that has different libraries in it.
            buildargs={"SANDBOX_TIER": settings.SANDBOX_TIER},
        )

    def _resolve_host_workspace(self) -> str:
        """Maps the workspace path into a value the Docker daemon can bind-mount.

        When the backend itself runs in a container, the path it sees is not the
        path the daemon sees, so the real host path is read back off our own
        container's mount table.
        """
        if _in_container():
            try:
                own = self.client.containers.get(socket.gethostname())
                for mount in own.attrs.get("Mounts", []):
                    if mount.get("Destination") == "/workspace":
                        source = mount.get("Source")
                        relative = self.workspace_dir.relative_to(settings.WORKSPACE_DIR)
                        return str(Path(source).joinpath(relative)) if str(relative) != "." else source
            except Exception as exc:
                logger.warning("Could not resolve host workspace path", error=str(exc))
        return str(self.workspace_dir)

    def _wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        host, port = self.endpoint()
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def _put_file(self, path: str, content: str):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name=os.path.basename(path))
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        stream.seek(0)
        if self.container is not None:
            self.container.put_archive(os.path.dirname(path), stream)

    # ------------------------------------------------------------------ #
    def interrupt(self) -> bool:
        """Signals the daemon process itself.

        The previous implementation targeted PID 1, which is the container's
        ``sleep`` command -- interrupting therefore killed the container instead
        of the running cell. The daemon writes its PID at startup so it can be
        signalled precisely.
        """
        if self.container is None:
            return False
        try:
            result = self.container.exec_run(
                ["sh", "-c", f"kill -INT $(cat {PID_FILE} 2>/dev/null) 2>/dev/null || true"]
            )
            logger.info("Sandbox interrupt signalled", session=self.session_id, exit_code=result.exit_code)
            return True
        except Exception as exc:
            logger.error("Failed to interrupt sandbox", error=str(exc))
            return False

    def stop(self):
        if self.container is None:
            return
        try:
            self.container.remove(force=True)
        except Exception as exc:
            logger.debug("Could not remove the sandbox container", session=self.session_id, error=str(exc))
        finally:
            self.container = None
            logger.info("Sandbox session stopped", session=self.session_id)


class SandboxPool:
    """Creates and reaps one :class:`SandboxSession` per user session."""

    def __init__(self):
        self._client = None
        self._client_checked = False
        self._sessions: dict[str, SandboxSession] = {}
        self._lock = threading.Lock()
        atexit.register(self.shutdown)

    # ------------------------------------------------------------------ #
    @property
    def client(self):
        """The Docker client, or None when Docker is unavailable/disabled."""
        if self._client_checked:
            return self._client
        with self._lock:
            if self._client_checked:
                return self._client
            self._client_checked = True
            if not settings.docker_backend_allowed:
                logger.info("Docker backend not selected", backend=settings.EXECUTION_BACKEND)
                self._client = None
                return None
            try:
                import docker

                client = docker.from_env()
                client.ping()
                self._client = client
                logger.info("Docker connection established")
            except Exception as exc:
                logger.warning("Docker unavailable; container execution disabled", error=str(exc))
                self._client = None
            return self._client

    @property
    def available(self) -> bool:
        return self.client is not None

    def workspace_for(self, session_id: str) -> Path:
        """Per-session workspace directory, created on demand.

        Lives here rather than on the session because every runtime -- container
        or subprocess -- reads its datasets out of this directory. Delegates to
        `runtime.resolve_workspace_dir` so a subagent's composite id resolves to
        a directory nested under its parent's rather than a flat, unrelated
        sibling -- imported locally to avoid a import cycle with `runtime`,
        which imports this pool lazily for the same reason.
        """
        from src.core.tools.runtime import resolve_workspace_dir

        return resolve_workspace_dir(session_id)

    def get(self, session_id: str, create: bool = True) -> SandboxSession | None:
        session = self._sessions.get(session_id)
        if session is not None or not create:
            return session

        if not self.available:
            return None

        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            session = SandboxSession(self.client, session_id, self.workspace_for(session_id))
            try:
                session.start()
            except Exception as exc:
                logger.error("Failed to start sandbox session", session=session_id, error=str(exc))
                session.stop()
                return None
            self._sessions[session_id] = session
            return session

    def release(self, session_id: str):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.stop()

    def prune_orphans(self):
        """Removes containers left behind by a previous process."""
        if not self.available:
            return
        try:
            for container in self.client.containers.list(all=True, filters={"label": "wizard_managed=true"}):
                if container.labels.get("wizard_session") in self._sessions:
                    continue
                try:
                    container.remove(force=True)
                except Exception as exc:
                    logger.debug(
                        "Could not remove an orphaned sandbox container", container=container.id, error=str(exc)
                    )
        except Exception as exc:
            logger.warning("Failed to prune orphaned sandboxes", error=str(exc))

    def shutdown(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()

    @property
    def active_count(self) -> int:
        return len(self._sessions)


sandbox_pool = SandboxPool()

__all__ = [
    "DAEMON_PATH",
    "DAEMON_PORT",
    "DAEMON_SCRIPT",
    "PID_FILE",
    "SandboxPool",
    "SandboxSession",
    "SandboxUnavailableError",
    "find_free_port",
    "sandbox_pool",
]
