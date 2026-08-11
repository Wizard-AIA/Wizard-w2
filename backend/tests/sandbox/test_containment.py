"""The sandbox, actually running. **Not part of the default suite.**

Every other test in this repository asserts on inert data, because the suite may
not spawn a process. These do spawn one, so they are skipped unless
``WIZARD_SANDBOX_SELFTEST=1`` is set, and CI never sets it.

Run them on each platform you care about:

    WIZARD_SANDBOX_SELFTEST=1 pytest backend/tests/sandbox -v

This is the only place the claim "generated code cannot write outside the
workspace" is checked against a kernel rather than against a docstring.
"""

from __future__ import annotations

import os

import pytest

from src.core.security.sandbox import capability, selftest


pytestmark = pytest.mark.skipif(
    os.environ.get("WIZARD_SANDBOX_SELFTEST") != "1",
    reason="spawns a real process; set WIZARD_SANDBOX_SELFTEST=1 to run",
)


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX", "best-effort")
    monkeypatch.setattr("src.config.settings.HOST_SANDBOX_NETWORK", "deny")
    return capability.detect()


def test_the_probe_runs_and_reports(enforced) -> None:
    """A self-test that cannot run tells the user nothing, which is its own bug."""
    result = selftest.run(timeout=120)

    assert result.checks, f"the probe produced no checks: {result.detail}"
    assert "workspace_write" in result.checks


def test_the_workspace_stays_writable(enforced) -> None:
    """Containment that also blocks the session's own directory is broken:
    the runtime could not write a chart or export a CSV.
    """
    result = selftest.run(timeout=120)

    assert result.checks["workspace_write"]["outcome"] == "allowed", result.checks["workspace_write"]["detail"]


def test_a_write_outside_the_workspace_is_blocked(enforced) -> None:
    supported = {feature.key: feature.supported for feature in enforced.features}
    if not supported.get("filesystem"):
        pytest.skip(f"filesystem containment is unavailable here: {enforced.features[0].detail}")

    result = selftest.run(timeout=120)

    assert result.checks["outside_write"]["outcome"] == "blocked", result.checks["outside_write"]["detail"]


def test_outbound_network_is_blocked(enforced) -> None:
    """Skipped on Windows, which reports this as unenforced rather than
    pretending — see `capability._windows_capability`.
    """
    supported = {feature.key: feature.supported for feature in enforced.features}
    if not supported.get("network"):
        pytest.skip("outbound network is not enforced on this platform")

    result = selftest.run(timeout=120)

    assert result.checks["outbound_network"]["outcome"] == "blocked", result.checks["outbound_network"]["detail"]


def test_a_memory_allocation_past_the_ceiling_is_blocked(enforced) -> None:
    """Regression for #123: on Windows the job object carrying the memory

    ceiling was created but never bound to the probe (`run()` used
    `subprocess.run`, which throws away the `Popen` handle `plan.adopt`
    needs), so this check always came back "allowed" there regardless of
    whether a real session's job object worked.
    """
    supported = {feature.key: feature.supported for feature in enforced.features}
    if not supported.get("memory"):
        pytest.skip(f"memory containment is unavailable here: {enforced.features[0].detail}")

    result = selftest.run(timeout=120)

    assert result.checks["memory_ceiling"]["outcome"] == "blocked", result.checks["memory_ceiling"]["detail"]


def test_the_verdict_agrees_with_the_checks(enforced) -> None:
    result = selftest.run(timeout=120)

    assert result.ok, result.detail
