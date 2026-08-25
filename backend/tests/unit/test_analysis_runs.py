"""Versioned analysis runs: an immutable snapshot, and the drift check that keeps a later export
honest about a dataset that changed underneath it (Phase 10, ADR 0005)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.analysis.runs import (
    AnalysisRun,
    DatasetManifestEntry,
    ExecutedStep,
    capture,
    dataset_manifest_from_session,
)


@pytest.fixture
def session():
    from src.core.session import session_manager

    created = session_manager.create()
    yield created
    session_manager.drop(created.id)


def test_dataset_manifest_from_session_captures_every_table(session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    session.add_dataset("b.csv", pd.DataFrame({"y": [1]}), make_active=False)

    manifest = dataset_manifest_from_session(session)

    names = {entry.name for entry in manifest}
    assert names == {"a.csv", "b.csv"}
    a_entry = next(entry for entry in manifest if entry.name == "a.csv")
    assert a_entry.rows == 3
    assert a_entry.columns == ("x",)
    assert a_entry.content_hash


def test_capture_builds_an_immutable_run_that_round_trips(session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))

    run = capture(
        session_id=session.id,
        message_id=7,
        instruction="how many rows",
        answer="3 rows.",
        dataset_manifest=dataset_manifest_from_session(session),
        steps=[ExecutedStep(goal="count", code="print(len(df))")],
        analysis={"evidence": {"nodes": [], "edges": []}},
        warnings=["a warning"],
    )

    data = run.to_dict()
    restored = AnalysisRun.from_dict(data)

    assert restored.session_id == session.id
    assert restored.message_id == 7
    assert restored.instruction == "how many rows"
    assert restored.steps[0].goal == "count"
    assert restored.warnings == ("a warning",)
    assert "python" in restored.tool_versions


def test_changed_since_is_empty_when_nothing_changed(session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    run = capture(
        session_id=session.id,
        message_id=1,
        instruction="q",
        answer="a",
        dataset_manifest=dataset_manifest_from_session(session),
        steps=[],
        analysis={},
        warnings=[],
    )

    assert run.changed_since(dataset_manifest_from_session(session)) == []


def test_changed_since_names_a_table_whose_content_hash_moved(session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    run = capture(
        session_id=session.id,
        message_id=1,
        instruction="q",
        answer="a",
        dataset_manifest=dataset_manifest_from_session(session),
        steps=[],
        analysis={},
        warnings=[],
    )

    session.add_dataset("a.csv", pd.DataFrame({"x": [99, 100, 101]}))

    assert run.changed_since(dataset_manifest_from_session(session)) == ["a.csv"]


def test_changed_since_ignores_a_table_the_run_never_saw() -> None:
    run = AnalysisRun(session_id="s", message_id=1, instruction="q", answer="a")

    current = [DatasetManifestEntry(name="new.csv", table_key="new", content_hash="abc", rows=1)]

    assert run.changed_since(current) == []
