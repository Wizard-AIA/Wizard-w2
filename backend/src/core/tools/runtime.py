"""Chooses which execution backend serves a session, and reports what it can do.

Two supported backends and one last resort:

===========  ====================================================================
host         A subprocess per session. The default: no image, no daemon to
             install, and still a separate process.
docker       A container per session. Opt-in; needs a daemon and an image.
inprocess    Guarded ``exec`` in the API process. No isolation; CI and locked-down
             environments only.
===========  ====================================================================

Docker is reached only when ``EXECUTION_BACKEND=docker`` names it. Asking for a
container and not having one degrades to ``host`` rather than failing, because a
missing daemon is a reason to run somewhere else, not a reason to stop -- but the
degradation is logged, since the user asked for something they did not get.

The choice is made per call rather than cached, because Docker can appear or
disappear while the app is running and the answer to "where does code run" has
to stay true rather than reflect what was true at boot.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache

from src.config import settings
from src.core.tools.daemon import PROBE_MODULES, DaemonClient


#: One of ``host`` / ``docker`` / ``inprocess``.
BackendName = str

#: Separates a parent session id from a subagent's branch name in a composite
#: id (Milestone 7). Distinct from anything `uuid4().hex` or a connection name
#: could produce, so `is_subagent_id` cannot false-positive on an ordinary id.
CHILD_DELIMITER = "::sub:"


def is_subagent_id(session_id: str) -> bool:
    return CHILD_DELIMITER in session_id


def parent_session_id(session_id: str) -> str:
    """The owning session's id, or ``session_id`` unchanged if it is not a child id."""
    return session_id.split(CHILD_DELIMITER, 1)[0]


def resolve_workspace_dir(session_id: str):
    """Where a session's (or a subagent's) files live on disk.

    A subagent is "a scoped child of the session, not a new top-level
    session" -- so its workspace nests under the parent's directory rather
    than sitting beside it as a flat, unrelated `sessions/<composite-id>`
    sibling, which is what a plain join would produce.
    """
    if is_subagent_id(session_id):
        parent, _, branch = session_id.partition(CHILD_DELIMITER)
        directory = settings.WORKSPACE_DIR / "sessions" / parent / "subagents" / branch
    else:
        directory = settings.WORKSPACE_DIR / "sessions" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def active_backend() -> BackendName:
    """Which backend a new session would use right now.

    ``inprocess`` is reported only when spawning is forbidden outright, which is
    an honest answer rather than a fallback hidden behind an availability flag.
    """
    if settings.EXECUTION_BACKEND == "inprocess":
        return "inprocess"

    if settings.docker_backend_allowed:
        from src.core.tools.sandbox import sandbox_pool

        if sandbox_pool.available:
            return "docker"
        # Asked for by name and not there. `sandbox_pool.available` has already
        # logged why, so this only records what it fell back to.
        from src.utils.logging import logger

        logger.warning("Docker was requested but is unreachable; running on the host backend")
        return "host"

    return "host" if settings.host_backend_allowed else "inprocess"


def get_runtime(session_id: str, create: bool = True) -> DaemonClient | None:
    """The live runtime for a session, or ``None`` when there is no daemon.

    A ``None`` here is not an error: it means execution falls through to the
    in-process interpreter, which :class:`~src.core.execution.CodeExecutor`
    handles.
    """
    backend = active_backend()

    if backend == "docker":
        from src.core.tools.sandbox import sandbox_pool

        return sandbox_pool.get(session_id, create=create)

    if backend == "host":
        from src.core.tools.host_runtime import host_runtime_pool

        runtime = host_runtime_pool.get(session_id, create=create)
        if runtime is not None or not create:
            return runtime
        # Spawning failed -- a locked-down host, no interpreter on PATH. The
        # in-process interpreter still works, so the session degrades instead
        # of failing outright.
        from src.utils.logging import logger

        logger.warning("Host runtime unavailable; falling back to in-process execution", session=session_id)
        return None

    return None


def release_runtime(session_id: str) -> None:
    """Releases whatever this session holds, in either backend.

    Both pools are asked rather than only the active one: the backend can change
    while a session is alive, and a container left behind by the previous choice
    would otherwise leak until the process exits.
    """
    from src.core.tools.host_runtime import host_runtime_pool
    from src.core.tools.sandbox import sandbox_pool

    sandbox_pool.release(session_id)
    host_runtime_pool.release(session_id)


def rebind_roots(session_id: str) -> bool:
    """Rebuilds a host runtime so a newly consented directory is inside its sandbox.

    The OS policy is fixed when the child starts -- a Landlock ruleset cannot be
    widened after ``restrict_self``, and neither can an SBPL profile or a lowered
    token. So a ``workspace_write`` grant made at iteration four reaches the AST
    guard but not the kernel, and the write the user was asked about and allowed
    fails anyway. Restarting is the only way to make the grant real.

    Only the host backend under an enforcing sandbox needs it. The daemon reloads
    the session's datasets and tables from the workspace on start, so what is
    lost is intermediate variables, not the data.
    """
    if active_backend() != "host" or settings.HOST_SANDBOX == "off":
        return False

    from src.core.tools.host_runtime import host_runtime_pool

    if host_runtime_pool.get(session_id, create=False) is None:
        return False
    host_runtime_pool.release(session_id)
    forget_capabilities(session_id)
    return True


def workspace_for(session_id: str):
    from src.core.tools.sandbox import sandbox_pool

    return sandbox_pool.workspace_for(session_id)


def workspace_path(session_id: str | None, filename: str = "") -> str:
    """A path inside the session workspace **as the running backend sees it**.

    A container is always at ``/workspace``; a host runtime works out of the
    session's own directory. Any code that builds a path for generated Python to
    write to has to go through here, because the two are not interchangeable:
    on the host backend ``/workspace/cleaned.csv`` resolves to a directory that
    does not exist, and pandas raises rather than writing anywhere useful.

    That is not hypothetical -- it silently disabled semantic cleaning and CSV
    export on every Docker-less install until it was caught by running one.
    """
    root = "/workspace"
    if active_backend() != "docker" and session_id:
        root = workspace_for(session_id).as_posix()
    return f"{root}/{filename}" if filename else f"{root}/"


def rebind_workspace_paths(text: str, session_id: str) -> str:
    """Rewrites another session's workspace path baked into ``text`` to this session's own.

    Cached code and retrieved trajectory/few-shot text can carry a literal
    absolute path resolved by ``workspace_path`` at the moment they were first
    generated -- that is exactly what the worker prompt hands the model to
    write to, and the model reproduces it literally. Replayed or imitated
    later by a *different* session, that path names a directory the new
    session does not own: best case the guard rejects it as out-of-root, worst
    case (if the original directory still exists) code writes into a workspace
    it has no claim to. Only the host backend embeds a session id in the path
    at all -- docker's ``/workspace`` root is the same string for every
    session, so there is nothing to rebind there.
    """
    if not text or active_backend() == "docker":
        return text
    root = settings.WORKSPACE_DIR.as_posix().rstrip("/")
    pattern = re.compile(re.escape(root) + r"/sessions/[^\s'\"/)]+(?:/subagents/[^\s'\"/)]+)?")
    replacement = workspace_path(session_id).rstrip("/")
    return pattern.sub(replacement, text)


def active_runtime_count() -> int:
    from src.core.tools.host_runtime import host_runtime_pool
    from src.core.tools.sandbox import sandbox_pool

    return sandbox_pool.active_count + host_runtime_pool.active_count


@lru_cache(maxsize=1)
def _local_modules() -> frozenset[str]:
    """Modules importable in *this* process, probed once.

    Used for the in-process interpreter, and as the answer for a host runtime
    that has not been started yet -- it is the same interpreter, so the set is
    the same without paying to spawn a child to be told so.
    """
    from importlib.util import find_spec

    available = set()
    for name in PROBE_MODULES:
        try:
            if find_spec(name) is not None:
                available.add(name)
        except (ImportError, ValueError, AttributeError):
            continue
    return frozenset(available)


#: Cached per session: a container's library set cannot change under it, and
#: this is consulted on every prompt build.
_capability_cache: dict[str, frozenset[str]] = {}


def capabilities(session_id: str | None = None) -> frozenset[str]:
    """Modules generated code may actually import, as reported by the runtime.

    This replaced a hand-maintained list in ``prompts.TOOLKIT`` that had to be
    edited in step with the Dockerfile and twice was not -- scikit-learn and
    statsmodels went unadvertised for months, and duckdb was advertised to a
    process that did not have it, which cost a correction retry every time.

    Never raises and never blocks for long: an unreachable runtime yields this
    process's own module set, which is the better wrong answer of the two.
    """
    backend = active_backend()
    if backend != "docker" or not session_id:
        return _local_modules()

    cached = _capability_cache.get(session_id)
    if cached is not None:
        return cached

    runtime = get_runtime(session_id, create=False)
    if runtime is None:
        # Do not start a container merely to build a prompt; the image is built
        # from a known tier, so the declared set is a sound answer until a real
        # runtime exists to correct it.
        return tier_modules()

    reported = runtime.capabilities()
    resolved = reported or tier_modules()
    _capability_cache[session_id] = resolved
    return resolved


def forget_capabilities(session_id: str) -> None:
    _capability_cache.pop(session_id, None)


def missing_modules(names: frozenset[str], session_id: str | None = None) -> frozenset[str]:
    """Which of ``names`` this runtime would have to install to run the code.

    Answered here because only the runtime knows what it is: a container reports
    its own module set through the daemon, while a host subprocess shares this
    process's interpreter, so asking ``find_spec`` here is the accurate test for
    one and the wrong test for the other.

    ``capabilities()`` probes a fixed list of analysis libraries, so it can say
    "no" about a module that is in fact installed. Off Docker the ``find_spec``
    pass corrects that; on Docker it is deliberately not consulted, because this
    process's modules say nothing about the container's.
    """
    candidates = {name for name in names if name and name not in sys.stdlib_module_names}
    if not candidates:
        return frozenset()

    candidates -= capabilities(session_id)
    if not candidates or active_backend() == "docker":
        return frozenset(candidates)

    from importlib.util import find_spec

    resolved = set()
    for name in candidates:
        try:
            if find_spec(name) is not None:
                resolved.add(name)
        except (ImportError, ValueError, AttributeError):
            continue
    return frozenset(candidates - resolved)


#: What each sandbox image tier installs. Mirrors `backend/docker/Dockerfile`,
#: and is only a fallback for "the container is not up yet" -- once a runtime
#: exists, its own report wins.
TIER_MODULES: dict[str, frozenset[str]] = {
    "core": frozenset({"pandas", "numpy", "pyarrow", "matplotlib", "duckdb", "polars", "openpyxl"}),
    "standard": frozenset(
        {
            "pandas",
            "numpy",
            "pyarrow",
            "matplotlib",
            "duckdb",
            "polars",
            "openpyxl",
            "scipy",
            "statsmodels",
            "sklearn",
            "xgboost",
            "lightgbm",
            "plotly",
            "seaborn",
            "networkx",
            "PIL",
            "xlsxwriter",
            "tabulate",
        }
    ),
}
TIER_MODULES["full"] = TIER_MODULES["standard"] | {"lifelines", "geopandas", "shapely"}


def tier_modules() -> frozenset[str]:
    return TIER_MODULES.get(settings.SANDBOX_TIER, TIER_MODULES["standard"])


__all__ = [
    "CHILD_DELIMITER",
    "TIER_MODULES",
    "active_backend",
    "active_runtime_count",
    "capabilities",
    "forget_capabilities",
    "get_runtime",
    "is_subagent_id",
    "parent_session_id",
    "rebind_roots",
    "release_runtime",
    "resolve_workspace_dir",
    "tier_modules",
    "workspace_for",
    "workspace_path",
]
