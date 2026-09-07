"""Regression tests for the reviewed offline benchmark baseline checker."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from benchmark_harness.baseline import compare, fingerprint_manifest, load_baseline


def test_reviewed_baseline_matches_the_current_offline_fixture_corpus() -> None:
    baseline = load_baseline(ROOT / "benchmarks" / "baselines" / "offline_adversarial_v1.json")

    assert compare(baseline, fingerprint_manifest(ROOT)) == []


def test_baseline_comparison_rejects_fixture_or_coverage_changes() -> None:
    current = {
        "schema_version": 1,
        "suite": "offline-adversarial",
        "scenarios": {"case": {"fixture_sha256": "new", "required_test": "test_case", "kind": "orchestrator"}},
    }
    expected = {
        "schema_version": 1,
        "suite": "offline-adversarial",
        "scenarios": {"case": {"fixture_sha256": "old", "required_test": "test_case", "kind": "orchestrator"}},
    }

    failures = compare(expected, current)

    assert len(failures) == 1
    assert "fixture_sha256" in failures[0]
