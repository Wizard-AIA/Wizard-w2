"""Stable identities for the offline analytical-quality benchmark corpus."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def _scenario_module(root: Path):
    harness = root / "scripts" / "benchmark_harness"
    if str(harness) not in sys.path:
        sys.path.insert(0, str(harness))
    import scenarios

    return scenarios


def _frame_payload(frame: Any) -> list[dict[str, Any]] | None:
    if frame is None:
        return None
    # `to_json` gives scalar values a stable, JSON-safe representation and the
    # following load avoids dataframe/NumPy implementation repr differences.
    return json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15))


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _loop_payload(scenario: Any) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "category": scenario.category,
        "instruction": scenario.instruction,
        "responses": scenario.responses,
        "tier": scenario.tier,
        "dataframe": _frame_payload(scenario.dataframe),
        "tables": {name: _frame_payload(frame) for name, frame in sorted(scenario.tables.items())},
    }


def fingerprint_manifest(root: Path) -> dict[str, Any]:
    """Return the source-controlled fixture and required-test manifest.

    The actual benchmark behaviour is exercised by pytest. This manifest gives
    review a deterministic answer to "did the corpus itself change?" without
    treating a mutable pass-rate summary as evidence.
    """
    scenarios = _scenario_module(root)
    entries: dict[str, dict[str, Any]] = {}
    for scenario in scenarios.LOOP_SCENARIOS:
        entries[scenario.id] = {
            "fixture_sha256": _digest(_loop_payload(scenario)),
            "required_test": f"test_{scenario.id}",
            "kind": "orchestrator",
        }

    deterministic = {
        "insufficient_evidence": {
            "questions": [],
            "expected_verdict": "insufficient_evidence",
        },
        "ambiguous_question": {
            "questions": list(scenarios.AMBIGUOUS_QUESTIONS),
            "unambiguous_question": scenarios.UNAMBIGUOUS_QUESTION,
        },
    }
    for scenario_id, payload in deterministic.items():
        entries[scenario_id] = {
            "fixture_sha256": _digest({"id": scenario_id, **payload}),
            "required_test": f"test_{scenario_id}",
            "kind": "deterministic",
        }

    return {"schema_version": 1, "suite": "offline-adversarial", "scenarios": entries}


def load_baseline(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("suite") != "offline-adversarial":
        raise ValueError("unsupported baseline schema or suite")
    if not isinstance(payload.get("scenarios"), dict):
        raise ValueError("baseline has no scenario mapping")
    return payload


def compare(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return human-actionable baseline mismatches in stable order."""
    failures: list[str] = []
    expected_scenarios = expected["scenarios"]
    current_scenarios = current["scenarios"]
    for scenario_id in sorted(set(expected_scenarios) | set(current_scenarios)):
        before = expected_scenarios.get(scenario_id)
        after = current_scenarios.get(scenario_id)
        if before is None:
            failures.append(f"new scenario '{scenario_id}' has no reviewed baseline entry")
            continue
        if after is None:
            failures.append(f"reviewed scenario '{scenario_id}' is missing")
            continue
        for field in ("fixture_sha256", "required_test", "kind"):
            if before.get(field) != after.get(field):
                failures.append(
                    f"scenario '{scenario_id}' changed {field}: "
                    f"expected {before.get(field)!r}, got {after.get(field)!r}"
                )
    return failures
