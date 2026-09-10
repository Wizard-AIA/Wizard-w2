"""Execution backends, the daemon source, and what the model is told it may import.

None of these start a container or spawn a subprocess. What is checked is the
part that used to be unverifiable: the daemon lives in a string literal, and the
toolkit the model is offered now comes from the runtime rather than from a list
maintained by hand against the Dockerfile.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.core.security.code_guard import CodeGuard
from src.core.tools import runtime as runtime_backend
from src.core.tools.daemon import PROBE_MODULES, render_daemon


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("setting", "expected"),
    [("inprocess", "inprocess"), ("host", "host"), ("docker", "inprocess")],
)
def test_backend_selection_follows_the_setting(monkeypatch, setting: str, expected: str) -> None:
    """An unavailable Docker daemon falls back to the requested compatibility
    mode, in-process execution, and readiness separately reports that it is
    unsafe for the production confinement profile.
    """
    monkeypatch.setattr("src.config.settings.EXECUTION_BACKEND", setting, raising=False)
    assert runtime_backend.active_backend() == expected


@pytest.mark.parametrize("legacy", ["auto", "local"])
def test_legacy_backend_names_fold_to_host(legacy: str) -> None:
    """A pre-w2 .env keeps working: `auto` and `local` both meant this backend."""
    from src.config import Settings

    assert Settings(EXECUTION_BACKEND=legacy).EXECUTION_BACKEND == "host"


def test_host_runtime_memory_limit_accepts_the_old_env_name(monkeypatch) -> None:
    """`LOCAL_RUNTIME_MEM_LIMIT` still sets the ceiling it always set."""
    from src.config import Settings

    monkeypatch.setenv("LOCAL_RUNTIME_MEM_LIMIT", "512m")
    assert Settings().HOST_RUNTIME_MEM_LIMIT == "512m"


# --------------------------------------------------------------------------- #
# The daemon source
# --------------------------------------------------------------------------- #
def test_daemon_renders_to_valid_python_for_every_runtime() -> None:
    for allow_pip in (True, False):
        for mem_bytes in (0, 1 << 30):
            source = render_daemon(allow_pip=allow_pip, mem_bytes=mem_bytes, bind_host="127.0.0.1")
            ast.parse(source)
            assert "%(" not in source


def _rendered_workspace(source: str) -> str:
    """Reads WORKSPACE back out of generated source, the way the daemon will."""
    line = next(ln for ln in source.splitlines() if ln.startswith("WORKSPACE"))
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - evaluating source this test just generated
    return namespace["WORKSPACE"]


@pytest.mark.parametrize(
    "workspace",
    [
        "C:\\Users\\a b\\workspace\\sessions\\x",
        "C:\\Users\\a\\workspace\\sessions\\abc",  # \\U and \\a are escape sequences
        "C:\\Users\\a\\",  # a trailing separator would escape the closing quote
        "/tmp/session-x",
        "/tmp/it's here",  # a quote would close the literal early
    ],
)
def test_a_workspace_path_survives_becoming_a_string_literal(workspace: str) -> None:
    """The local runtime passes a real host path into generated source.

    On Windows that path contains backslashes, which are escape sequences: `\\t`
    in a username is a tab, and `C:\\Users` is a truncated `\\U` escape that will
    not parse at all.

    This is asserted as a round-trip rather than by looking for a particular
    spelling in the text. The previous fix ran the path through
    `Path.as_posix()` and the test checked for forward slashes -- but
    `as_posix()` only rewrites separators *on Windows*. On Linux and macOS a
    Windows path is one opaque filename, the backslashes survived, and the
    daemon was unparseable on exactly the platforms CI runs.
    """
    source = render_daemon(workspace=workspace)
    ast.parse(source)
    assert _rendered_workspace(source) == workspace


def test_the_daemon_reads_tables_from_its_own_workspace() -> None:
    """The path was hardcoded to /workspace, which only exists in a container."""
    source = render_daemon(workspace="/tmp/session-x")
    assert _rendered_workspace(source) == "/tmp/session-x"
    assert '"/workspace/tables"' not in source


# --------------------------------------------------------------------------- #
# Capabilities: what the model is told it may import
# --------------------------------------------------------------------------- #
DOCKERFILE = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile"

#: Distribution name -> import name, for the few that differ.
IMPORT_NAMES = {
    "scikit-learn": "sklearn",
    "xgboost-cpu": "xgboost",
    "pillow": "PIL",
}


def _packages_per_tier() -> dict[str, set[str]]:
    """Reads the pinned packages out of each tier's layer in the Dockerfile."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    tiers = {"core": set(), "standard": set(), "full": set()}
    current = "core"
    for line in text.splitlines():
        if line.startswith("RUN if") and 'SANDBOX_TIER" = "full"' in line:
            current = "full"
        elif line.startswith("RUN if") and 'SANDBOX_TIER" != "core"' in line:
            current = "standard"
        elif line.startswith("RUN $PIP"):
            current = "core"
        match = re.search(r'"([A-Za-z0-9_.-]+)==', line)
        if match:
            name = match.group(1).lower()
            tiers[current].add(IMPORT_NAMES.get(name, name))
    # Tiers are cumulative in the image, so they are cumulative here too.
    tiers["standard"] |= tiers["core"]
    tiers["full"] |= tiers["standard"]
    return tiers


def test_declared_tiers_match_what_the_dockerfile_installs() -> None:
    """The prompt's library list and the image drifted apart twice before.

    scikit-learn and statsmodels were installed and unadvertised for months, so
    generated code hand-rolled statistics that were already there; duckdb was
    advertised into a process without it, costing a correction retry every time.
    The runtime now reports its own modules, but this table is what a prompt is
    built from before any runtime exists -- so it has to stay true.
    """
    installed = _packages_per_tier()
    for tier, modules in runtime_backend.TIER_MODULES.items():
        assert modules == installed[tier], f"{tier} tier disagrees with the Dockerfile"


def test_every_declared_module_is_one_the_daemon_probes_for() -> None:
    """A module absent from PROBE_MODULES can never be reported as available."""
    for modules in runtime_backend.TIER_MODULES.values():
        assert modules <= set(PROBE_MODULES)


def test_the_toolkit_block_only_offers_what_is_importable(monkeypatch) -> None:
    from src.core import prompts

    monkeypatch.setattr(prompts, "_toolkit_block", prompts._toolkit_block)
    monkeypatch.setattr(
        "src.core.tools.runtime.capabilities",
        lambda session_id=None: frozenset({"pandas", "numpy", "matplotlib"}),
    )
    block = prompts._toolkit_block("session")

    assert "pandas" in block
    assert "duckdb" not in block
    assert "scikit-learn" not in block
    assert "Do not import a library that is not listed above." in block


def test_plotting_rules_do_not_ask_for_a_library_that_is_absent(monkeypatch) -> None:
    """PLOT_FORMAT=html needs plotly, and the `core` image tier has no plotly.

    Telling the worker to import it there would guarantee a failed step on the
    first chart of every run.
    """
    from src.core import prompts

    monkeypatch.setattr("src.config.settings.PLOT_FORMAT", "html", raising=False)
    monkeypatch.setattr(
        "src.core.tools.runtime.capabilities",
        lambda session_id=None: frozenset({"pandas", "matplotlib"}),
    )
    assert "matplotlib" in prompts._visualization_rules("session")

    monkeypatch.setattr(
        "src.core.tools.runtime.capabilities",
        lambda session_id=None: frozenset({"pandas", "matplotlib", "plotly"}),
    )
    assert "plotly" in prompts._visualization_rules("session").lower()


# --------------------------------------------------------------------------- #
# The guard has to follow the runtime's workspace
# --------------------------------------------------------------------------- #
def test_the_guard_accepts_the_workspace_the_host_runtime_actually_uses() -> None:
    """Without this the guard rejects the chart path the prompt itself supplied.

    A container works out of /workspace; a host runtime works out of the
    session's own directory, which on Windows is a drive-letter path that
    `posixpath.isabs` does not recognise.
    """
    root = "C:/3rd_Year/Wizard-w1/workspace/sessions/abc"
    code = f"df.to_csv('{root}/out.csv')"

    assert CodeGuard.scan(code).ok is False
    assert CodeGuard.scan(code, extra_roots=(root,)).ok is True


def test_widening_the_workspace_does_not_widen_anything_else() -> None:
    allowed = ("/srv/session",)
    assert CodeGuard.scan("df.to_csv('/etc/passwd')", extra_roots=allowed).ok is False
    assert CodeGuard.scan("open('/srv/session/../../etc/shadow')", extra_roots=allowed).ok is False
    # /workspace stays valid: the extra root is added to the list, not swapped in.
    assert CodeGuard.scan("df.to_csv('/workspace/out.csv')", extra_roots=allowed).ok is True


# --------------------------------------------------------------------------- #
# A consented directory has to reach the kernel, not just the guard
# --------------------------------------------------------------------------- #
class _StoppableRuntime:
    is_running = True

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_a_new_root_restarts_the_runtime_the_sandbox_already_fixed(monkeypatch) -> None:
    """A Landlock ruleset cannot be widened after `restrict_self`, so consent
    given at iteration four reaches the AST guard and not the kernel. Without a
    restart the user is asked, says yes, and the write still fails.
    """
    from src.core.tools.host_runtime import host_runtime_pool

    monkeypatch.setattr("src.config.settings.EXECUTION_BACKEND", "host")
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    runtime = _StoppableRuntime()
    monkeypatch.setitem(host_runtime_pool._sessions, "s1", runtime)

    assert runtime_backend.rebind_roots("s1") is True
    assert runtime.stopped is True
    assert host_runtime_pool.get("s1", create=False) is None


def test_nothing_is_restarted_when_no_sandbox_fixed_the_roots(monkeypatch) -> None:
    """With the OS sandbox off the child never had a policy to widen, so paying
    for a restart would throw away the namespace for nothing.
    """
    from src.core.tools.host_runtime import host_runtime_pool

    monkeypatch.setattr("src.config.settings.EXECUTION_BACKEND", "host")
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "off")
    runtime = _StoppableRuntime()
    monkeypatch.setitem(host_runtime_pool._sessions, "s2", runtime)

    assert runtime_backend.rebind_roots("s2") is False
    assert runtime.stopped is False


# --------------------------------------------------------------------------- #
# Subagent composite ids (Milestone 7)
# --------------------------------------------------------------------------- #
def test_is_subagent_id_recognises_the_delimiter() -> None:
    assert runtime_backend.is_subagent_id("abc123::sub:sub1") is True
    assert runtime_backend.is_subagent_id("abc123") is False


def test_parent_session_id_strips_the_branch() -> None:
    assert runtime_backend.parent_session_id("abc123::sub:sub1") == "abc123"
    # An ordinary id is returned unchanged rather than raising.
    assert runtime_backend.parent_session_id("abc123") == "abc123"


def test_resolve_workspace_dir_nests_a_subagent_under_its_parent() -> None:
    """A subagent is a scoped child, not a new top-level session.

    A flat join (`sessions/<composite-id>`) would produce an unrelated
    sibling directory; this must nest under the parent's own directory
    instead, so a subagent is visibly and actually part of one session.
    """
    parent = runtime_backend.resolve_workspace_dir("abc123")
    child = runtime_backend.resolve_workspace_dir("abc123::sub:sub1")

    assert child != parent
    assert child.parent.name == "subagents"
    assert child.parent.parent == parent
    assert child.name == "sub1"
    assert child.is_dir()  # created on demand, like the parent's own workspace
