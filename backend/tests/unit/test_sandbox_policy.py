"""The OS sandbox, everything about it that can be checked without a process.

Nothing here spawns anything, applies a syscall filter or touches a kernel
interface — the suite's rule that no test spawns a process holds, and the parts
that genuinely need a child are in `backend/tests/sandbox/`, which only runs
when `WIZARD_SANDBOX_SELFTEST=1` is set.

What is left is most of what there is to get wrong: a writable root that escapes
the workspace is a policy bug rather than a syscall bug, and the macOS profile
is a string that no Windows or Linux developer would otherwise ever read.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.core.security.sandbox import capability, policy as policy_module
from src.core.security.sandbox.bootstrap import render_bootstrap
from src.core.security.sandbox.policy import CACHE_ENV_VARS, SandboxPolicy, policy_for
from src.core.security.sandbox.profiles import sbpl_profile
from src.core.security.sandbox.spawn import SandboxUnavailableError, plan_spawn
from src.core.tools.packages import install as real_install


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "sessions" / "abc"
    directory.mkdir(parents=True)
    return directory


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_the_only_writable_root_is_the_workspace(workspace: Path, monkeypatch) -> None:
    """The whole boundary reduces to this. A readable root is the interpreter;
    a writable one that is not the session's own directory is an escape.
    """
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    built = policy_for(workspace)

    assert built.writable == (str(workspace),)
    assert str(workspace) not in built.readable


def test_consented_roots_widen_the_sandbox(workspace: Path, monkeypatch) -> None:
    """A `workspace_write` grant reaches here as well as the AST guard.

    The two have to agree: the guard is told yes and the sandbox is not, the
    write still fails, and the consent the user gave reads as broken.
    """
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    granted = str(workspace.parent / "reports")
    built = policy_for(workspace, (granted,))

    assert built.writable == (str(workspace), granted)


def test_cache_environment_redirects_every_variable_into_the_workspace(workspace: Path, monkeypatch) -> None:
    """Matplotlib and fontconfig write to the user's home, which no writable
    root covers — so without this the child dies on `import matplotlib`, before
    any generated code exists.
    """
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    built = policy_for(workspace)
    environment = built.cache_environment()

    assert set(environment) == set(CACHE_ENV_VARS)
    assert all(value.startswith(str(workspace)) for value in environment.values())


def test_policy_survives_the_round_trip_to_the_child() -> None:
    """It travels as JSON, so anything not JSON-safe would fail at spawn."""
    built = SandboxPolicy(writable=("/w",), readable=("/usr",), mem_bytes=1 << 30, cache_dir="/w/.cache")
    restored = SandboxPolicy.from_dict(json.loads(json.dumps(built.to_dict())))

    assert restored == built


def test_interpreter_roots_are_readable_but_never_writable(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    built = policy_for(workspace)

    assert built.readable, "the child cannot import anything without these"
    assert not set(built.readable) & set(built.writable)


# --------------------------------------------------------------------------- #
# The macOS profile
# --------------------------------------------------------------------------- #
def test_sbpl_profile_denies_by_default_and_allows_only_loopback() -> None:
    """Deny-by-default is the whole point: a profile that starts from
    `(allow default)` and subtracts stops containing anything the day the OS
    grows a capability it does not know to subtract.
    """
    profile = sbpl_profile(
        SandboxPolicy(writable=("/work",), readable=("/usr",), network="deny", cache_dir="/work/.cache")
    )

    assert profile.startswith("(version 1)\n(deny default)\n")
    assert '(allow file-read* (subpath "/usr"))' in profile
    assert '(allow file-read* file-write* (subpath "/work"))' in profile
    assert "(deny network*)" in profile
    assert '(allow network-outbound (remote ip "localhost:*"))' in profile
    assert "(allow network*)" not in profile.replace("(deny network*)", "")


def test_sbpl_profile_opens_the_network_only_when_configured_to() -> None:
    profile = sbpl_profile(SandboxPolicy(writable=("/work",), readable=("/usr",), network="allow"))

    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_sbpl_profile_escapes_a_quote_in_a_path() -> None:
    """SBPL is Scheme; an unescaped quote in a directory name would end the
    string and the rest of the path would be read as code.
    """
    profile = sbpl_profile(SandboxPolicy(writable=('/work/od"d',), readable=()))

    assert r"\"" in profile


# --------------------------------------------------------------------------- #
# Capability reporting
# --------------------------------------------------------------------------- #
def test_every_feature_carries_a_reason() -> None:
    """ "Not enforced" with no reason is the failure this milestone exists to
    avoid — the user cannot act on a blank.
    """
    detected = capability.detect()

    assert {feature.key for feature in detected.features} == set(capability.FEATURES)
    assert all(feature.detail for feature in detected.features)


def test_windows_states_that_network_is_not_enforced() -> None:
    """Reported rather than left blank or quietly claimed. WFP needs
    administrator and AppContainer would require re-ACLing the user's Python.
    """
    detected = capability._windows_capability()
    network = next(feature for feature in detected.features if feature.key == "network")

    assert network.supported is False
    assert "WFP" in network.detail or "AppContainer" in network.detail
    assert all(
        feature.supported for feature in detected.features if feature.key in ("filesystem", "memory", "processes")
    )


def test_macos_capability_is_probed_not_inferred_from_the_version(monkeypatch) -> None:
    """`sandbox-exec` has been deprecated since 10.14 and still works well past
    it, so a version check would refuse a mechanism that functions. Both probes
    are stubbed here — running either would spawn a process.
    """
    monkeypatch.setattr(capability, "_probe_sandbox_exec", lambda: (False, "sandbox-exec is not present"))
    monkeypatch.setattr(capability, "_probe_rlimit_as", lambda: (True, "RLIMIT_AS"))
    detected = capability._macos_capability()

    assert detected.mechanism == "sandbox-exec"
    assert not detected.features[0].supported
    assert "not present" in detected.features[0].detail


def test_macos_memory_is_probed_not_assumed(monkeypatch) -> None:
    """Regression: `_macos_capability` used to claim ``memory`` supported

    unconditionally (`Feature("memory", True, "RLIMIT_AS")`), the same kind of
    optimistic claim #123 flagged on Windows. `setrlimit(RLIMIT_AS, ...)` is
    refused outright on some macOS hosts (observed on GitHub's `macos-latest`
    runners) — a static claim there made `_judge` trust an allocation that
    actually sailed straight through. Stubbed here — running the real probe
    would spawn a process.
    """
    monkeypatch.setattr(capability, "_probe_sandbox_exec", lambda: (True, "sandbox-exec accepts a profile"))
    monkeypatch.setattr(capability, "_probe_rlimit_as", lambda: (False, "RLIMIT_AS was refused on this host"))
    detected = capability._macos_capability()

    memory = next(feature for feature in detected.features if feature.key == "memory")
    assert memory.supported is False
    assert "refused" in memory.detail


# --------------------------------------------------------------------------- #
# Spawn planning
# --------------------------------------------------------------------------- #
def test_sandbox_off_leaves_the_command_line_alone(workspace: Path) -> None:
    argv = ["python", "daemon.py"]
    plan = plan_spawn(SandboxPolicy(writable=(), readable=(), mode="off"), argv, workspace)

    assert plan.argv == argv
    assert plan.mechanism == "none"


def test_require_refuses_rather_than_downgrading(workspace: Path, monkeypatch) -> None:
    """Someone who set `require` asked for a boundary. Handing them a plain
    subprocess that looks like one is the wrong answer to give silently.
    """
    monkeypatch.setattr(
        capability,
        "detect",
        lambda: capability.SandboxCapability(
            platform="test",
            mechanism="none",
            features=tuple(capability.Feature(name, False, "nothing here") for name in capability.FEATURES),
        ),
    )
    monkeypatch.setattr("src.core.security.sandbox.spawn.detect", capability.detect)

    with pytest.raises(SandboxUnavailableError):
        plan_spawn(SandboxPolicy(writable=(), readable=(), mode="require"), ["python"], workspace)


def test_best_effort_runs_anyway_when_nothing_is_available(workspace: Path, monkeypatch) -> None:
    """A 5.10 kernel has no Landlock and must still be able to answer a question."""
    monkeypatch.setattr(
        "src.core.security.sandbox.spawn.detect",
        lambda: capability.SandboxCapability(
            platform="test",
            mechanism="none",
            features=tuple(capability.Feature(name, False, "nothing here") for name in capability.FEATURES),
        ),
    )
    plan = plan_spawn(
        SandboxPolicy(writable=(), readable=(), mode="best-effort", cache_dir=str(workspace / ".cache")),
        ["python"],
        workspace,
    )

    assert plan.argv == ["python"]
    assert plan.env, "the cache redirection applies even with no OS mechanism"


# --------------------------------------------------------------------------- #
# The bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_renders_to_valid_python_with_a_windows_path(workspace: Path) -> None:
    """A Windows path inside a quoted literal is a set of escape sequences —
    `C:\\Users` is a truncated `\\U`. Rendered with %r for the same reason the
    daemon is.
    """
    built = SandboxPolicy(writable=("C:\\Users\\a\\ws",), readable=("C:\\Python313",), cache_dir="C:\\Users\\a\\ws")
    source = render_bootstrap(built, "C:\\Users\\a\\ws\\.runtime_daemon.py")

    ast.parse(source)
    assert "%(" not in source


def test_bootstrap_carries_the_policy_intact(workspace: Path) -> None:
    built = SandboxPolicy(writable=("/w",), readable=("/usr",), network="deny", mem_bytes=1 << 30)
    source = render_bootstrap(built, "/w/.runtime_daemon.py")

    line = next(ln for ln in source.splitlines() if ln.startswith("POLICY"))
    namespace: dict = {"json": json}
    exec(line, namespace)  # noqa: S102 - evaluating source this test just generated

    assert SandboxPolicy.from_dict(namespace["POLICY"]) == built


def test_bootstrap_points_at_a_child_module_that_exists() -> None:
    """It is loaded by path, so a rename would only surface at spawn."""
    source = render_bootstrap(SandboxPolicy(writable=(), readable=()), "/w/daemon.py")
    line = next(ln for ln in source.splitlines() if ln.startswith("CHILD_MODULE"))
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - evaluating source this test just generated

    assert Path(namespace["CHILD_MODULE"]).is_file()


def test_installs_are_refused_when_on_demand_pip_is_disabled(tmp_path: Path, monkeypatch) -> None:
    """Consent now performs the install, in the parent, so this switch is the
    only thing between an approved gate and a real index.

    It is not hypothetical: the suite reached PyPI once, for six hundred
    seconds, before this check existed. `conftest.py` pins the setting off; this
    pins the behaviour that pin relies on.
    """
    from src.core.tools.packages import LIBS_DIRNAME

    monkeypatch.setattr("src.config.settings.SANDBOX_ALLOW_RUNTIME_PIP", False)

    # `real_install` is bound at import time, before conftest's autouse stub
    # replaces the module attribute — this test is the one place that must
    # exercise the real function.
    ok, detail = real_install(tmp_path, {"requests"})

    assert ok is False
    assert "SANDBOX_ALLOW_RUNTIME_PIP" in detail
    assert not (tmp_path / LIBS_DIRNAME).exists()


def test_a_flag_injection_attempt_is_refused_before_pip_runs(tmp_path: Path, monkeypatch) -> None:
    """A module name reaching `pip install <name>` as a bare positional arg is
    read by pip's own argument parser -- a leading dash or a `pkg @ url` direct
    reference is not neutralised by `subprocess.run` avoiding a shell.
    """
    monkeypatch.setattr("src.config.settings.SANDBOX_ALLOW_RUNTIME_PIP", True)

    ok, detail = real_install(tmp_path, {"--index-url=http://evil.example.com"})

    assert ok is False
    assert "not a valid package name" in detail


def test_a_vcs_direct_reference_is_refused_before_pip_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.config.settings.SANDBOX_ALLOW_RUNTIME_PIP", True)

    ok, detail = real_install(tmp_path, {"innocuous @ git+https://evil.example.com/repo"})

    assert ok is False
    assert "not a valid package name" in detail


def test_the_distribution_map_is_shared_with_the_daemon() -> None:
    """Two copies would drift, and the container would install a different
    package than the parent for the same import name.
    """
    from src.core.tools.daemon import render_daemon
    from src.core.tools.packages import DISTRIBUTION_NAMES

    source = render_daemon(workspace="/workspace")

    assert "xgboost-cpu" in source
    assert DISTRIBUTION_NAMES["sklearn"] == "scikit-learn"
    assert '"sklearn": "scikit-learn"' in source


# --------------------------------------------------------------------------- #
# The self-test's own judgement
# --------------------------------------------------------------------------- #
def _capability(**supported) -> object:
    return capability.SandboxCapability(
        platform="test",
        mechanism="test",
        features=tuple(capability.Feature(name, supported.get(name, True), "test") for name in capability.FEATURES),
    )


def test_the_probe_script_is_valid_python() -> None:
    """It is a string literal rendered with %, so a syntax error in it would
    only ever surface as a child that produced no report.
    """
    from src.core.security.sandbox.selftest import PROBE_SCRIPT

    source = PROBE_SCRIPT % {"workspace": "/w", "outside": "/home/u/x", "mem_bytes": 1 << 30}

    ast.parse(source)
    assert "%(" not in source


def test_an_unblocked_escape_fails_the_verdict() -> None:
    from src.core.security.sandbox.selftest import _judge

    ok, detail = _judge(
        {
            "workspace_write": {"outcome": "allowed", "detail": ""},
            "outside_write": {"outcome": "allowed", "detail": ""},
        },
        _capability(),
    )

    assert ok is False
    assert "outside the workspace" in detail


def test_a_feature_the_platform_cannot_enforce_does_not_fail_the_verdict() -> None:
    """Windows cannot block outbound network and says so. Failing the whole
    self-test for it would train the reader to ignore a red result on exactly
    the machines that have a real one.
    """
    from src.core.security.sandbox.selftest import _judge

    ok, _ = _judge(
        {
            "workspace_write": {"outcome": "allowed", "detail": ""},
            "outside_write": {"outcome": "blocked", "detail": ""},
            "outbound_network": {"outcome": "allowed", "detail": ""},
        },
        _capability(network=False),
    )

    assert ok is True


def test_a_workspace_the_sandbox_denies_is_a_failure() -> None:
    """Containment that blocks the session's own directory is broken, not strict —
    the runtime could not write a chart or export a CSV.
    """
    from src.core.security.sandbox.selftest import _judge

    ok, detail = _judge(
        {
            "workspace_write": {"outcome": "blocked", "detail": ""},
            "outside_write": {"outcome": "blocked", "detail": ""},
        },
        _capability(),
    )

    assert ok is False
    assert "not writable" in detail


def test_an_inconclusive_network_check_is_not_reported_as_blocked() -> None:
    """The probe dials a TEST-NET address, so a timeout proves nothing either
    way. Counting it as a pass would be the invented claim this layer exists to
    avoid — but it is not a failure either.
    """
    from src.core.security.sandbox.selftest import _judge

    ok, _ = _judge(
        {
            "workspace_write": {"outcome": "allowed", "detail": ""},
            "outside_write": {"outcome": "blocked", "detail": ""},
            "outbound_network": {"outcome": "inconclusive", "detail": ""},
        },
        _capability(),
    )

    assert ok is True


def test_an_unblocked_memory_allocation_fails_the_verdict() -> None:
    """The probe allocates past the configured ceiling; a platform that claims

    to enforce ``memory`` and lets it through is exactly the false-positive
    `#123 <https://github.com/Wizard-AIA/Wizard-w2/issues/123>`_ reported: a
    Windows job object that was created but never bound to the child, so
    ``memory_ceiling`` came back ``"allowed"`` while `_judge` silently
    ignored it.
    """
    from src.core.security.sandbox.selftest import _judge

    ok, detail = _judge(
        {
            "workspace_write": {"outcome": "allowed", "detail": ""},
            "outside_write": {"outcome": "blocked", "detail": ""},
            "memory_ceiling": {"outcome": "allowed", "detail": ""},
        },
        _capability(),
    )

    assert ok is False
    assert "memory" in detail


def test_an_uncapped_memory_check_is_not_reported_as_blocked() -> None:
    """``mem_bytes=0`` (no ceiling configured) records ``inconclusive``, which

    must not read as a failure — there was nothing to enforce.
    """
    from src.core.security.sandbox.selftest import _judge

    ok, _ = _judge(
        {
            "workspace_write": {"outcome": "allowed", "detail": ""},
            "outside_write": {"outcome": "blocked", "detail": ""},
            "memory_ceiling": {"outcome": "inconclusive", "detail": ""},
        },
        _capability(),
    )

    assert ok is True


def test_run_binds_the_probe_into_its_job_object(monkeypatch, tmp_path: Path) -> None:
    """`run()` used to call `subprocess.run`, which has no way to hand the

    spawned child back to `plan.adopt` -- the call that puts a Windows probe
    into the job object carrying its memory/process ceilings
    (`windows.assign_to_job`). The job was built with the right limits and
    then simply never attached, so the probe ran unconstrained and the
    self-test reported memory/process containment as absent even when a real
    session (`HostSession`, which does call `adopt`) enforces both.

    No real process (or `icacls`) is spawned here (this file's rule):
    `subprocess.Popen` is stubbed with a fake that satisfies the
    `communicate()`/`returncode` surface `run()` uses, and `plan_spawn` is
    stubbed too so building the plan cannot itself shell out. What is pinned
    is purely that `run()` calls `plan.adopt` with whatever `Popen` returned.
    """
    from src.core.security.sandbox import selftest as selftest_module
    from src.core.security.sandbox.spawn import SpawnPlan

    monkeypatch.setattr("src.config.settings.WORKSPACE_DIR", tmp_path)

    class _FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return b"WIZARD_SELFTEST {}\n", b""

    adopted = []
    monkeypatch.setattr(selftest_module.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(
        selftest_module,
        "plan_spawn",
        lambda policy, argv, workspace: SpawnPlan(
            argv=argv, adopt=lambda process: adopted.append(process), mechanism="test"
        ),
    )

    selftest_module.run(timeout=10)

    assert len(adopted) == 1
    assert isinstance(adopted[0], _FakeProcess)


def test_the_child_module_imports_nothing_from_src() -> None:
    """It is loaded by file path from a process that has no `src` on its path,
    and a sandbox that has denied the repository cannot import its own bootstrap.
    """
    source = Path(policy_module.__file__).with_name("child.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "src" not in imported


# --------------------------------------------------------------------------- #
# The bootstrap-to-daemon handoff
# --------------------------------------------------------------------------- #
def test_the_daemon_reads_the_seal_off_the_builtins_module() -> None:
    """`runpy.run_path` binds `__builtins__` in the script's globals to a *dict*,
    so `getattr(__builtins__, "__wizard_seal__")` finds nothing and the network
    filter is never installed — silently, on every real session, while the
    self-test still reports it blocked because the probe calls the seal itself.
    """
    from src.core.tools.daemon import DAEMON_SCRIPT

    assert "\nimport builtins\n" in DAEMON_SCRIPT
    assert 'getattr(builtins, "__wizard_seal__"' in DAEMON_SCRIPT
    assert "getattr(__builtins__," not in DAEMON_SCRIPT


def test_the_bootstrap_leaves_both_phases_where_the_daemon_looks(workspace: Path) -> None:
    policy = SandboxPolicy(writable=(str(workspace),), readable=())
    source = render_bootstrap(policy, workspace / "daemon.py")

    assert "builtins.__wizard_sandbox__" in source
    assert "builtins.__wizard_seal__" in source
