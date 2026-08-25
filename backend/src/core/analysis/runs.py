"""Versioned analysis runs -- PLAN.md Layer 8 / ADR 0005.

Every other structure in this package (`AnalyticalState`, `EvidenceGraph`, `HypothesisSet`, ...)
is mutable working state, live for exactly one turn. `AnalysisRun` is the opposite by design: an
immutable snapshot captured once, at `_finalize`, of everything a report or export would need --
which datasets were active (by content hash, not a live reference that can later point somewhere
else), which code actually ran, and the turn's full analytical state (plan, evidence, hypotheses,
validations, critic findings, confidence). A report renders from a run id, never from mutable live
state, so re-exporting turn N after turn N+1 replaced a dataset still reflects what turn N actually
saw -- PLAN.md §15: "The user must never receive a report that silently refers to state modified by
a later analysis."

Dataset *bytes* are not duplicated here, only content hashes -- the same lineage fingerprint
`provenance.py`'s dataset nodes already use. A run is a record of what happened and what was true
of the data at the time, not a second copy of the data itself.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from src.core.session import Session


@dataclass(frozen=True)
class DatasetManifestEntry:
    """One table's identity at the moment a run captured it."""

    name: str
    table_key: str
    content_hash: str
    rows: int
    columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table_key": self.table_key,
            "content_hash": self.content_hash,
            "rows": self.rows,
            "columns": list(self.columns),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetManifestEntry:
        return cls(
            name=str(data.get("name", "")),
            table_key=str(data.get("table_key", "")),
            content_hash=str(data.get("content_hash", "")),
            rows=int(data.get("rows", 0)),
            columns=tuple(data.get("columns") or ()),
        )


@dataclass(frozen=True)
class ExecutedStep:
    """One step's code and outcome, frozen at the moment it ran."""

    goal: str
    code: str
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "code": self.code, "ok": self.ok}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutedStep:
        return cls(goal=str(data.get("goal", "")), code=str(data.get("code", "")), ok=bool(data.get("ok", True)))


@dataclass(frozen=True)
class AnalysisRun:
    """An immutable snapshot of one turn -- everything a report or export renders from."""

    session_id: str
    message_id: int
    instruction: str
    answer: str
    dataset_manifest: tuple[DatasetManifestEntry, ...] = ()
    steps: tuple[ExecutedStep, ...] = ()
    #: `AnalyticalState.to_dict()` at `_finalize` time -- plan, evidence, hypotheses, validations,
    #: critic findings, confidence, all in one place already, so it is carried whole rather than
    #: re-decomposed into more columns this module would have to keep in sync with `state.py`.
    analysis: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    tool_versions: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "instruction": self.instruction,
            "answer": self.answer,
            "dataset_manifest": [entry.to_dict() for entry in self.dataset_manifest],
            "steps": [step.to_dict() for step in self.steps],
            "analysis": self.analysis,
            "warnings": list(self.warnings),
            "tool_versions": self.tool_versions,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisRun:
        return cls(
            session_id=str(data.get("session_id", "")),
            message_id=int(data.get("message_id", 0)),
            instruction=str(data.get("instruction", "")),
            answer=str(data.get("answer", "")),
            dataset_manifest=tuple(
                DatasetManifestEntry.from_dict(entry) for entry in data.get("dataset_manifest") or ()
            ),
            steps=tuple(ExecutedStep.from_dict(step) for step in data.get("steps") or ()),
            analysis=data.get("analysis") or {},
            warnings=tuple(data.get("warnings") or ()),
            tool_versions=dict(data.get("tool_versions") or {}),
            created_at=float(data.get("created_at") or 0.0),
        )

    def changed_since(self, current_manifest: list[DatasetManifestEntry]) -> list[str]:
        """Table names whose content hash no longer matches what this run recorded -- the check
        that keeps a later export honest about drift rather than silently serving stale-mismatched
        data (dataset *bytes* are not versioned here, only their fingerprint at capture time)."""
        current_by_name = {entry.name: entry.content_hash for entry in current_manifest}
        return [
            entry.name
            for entry in self.dataset_manifest
            if entry.name in current_by_name and current_by_name[entry.name] != entry.content_hash
        ]


def dataset_manifest_from_session(session: Session) -> list[DatasetManifestEntry]:
    """The content-hash manifest of every table active in ``session`` right now. Called once, at
    the moment a run is captured -- a session's tables can be replaced by a later turn, and this
    is what lets `AnalysisRun.changed_since` notice."""
    return [
        DatasetManifestEntry(
            name=handle.name,
            table_key=handle.table_key,
            content_hash=handle.content_hash,
            rows=int(len(handle.df)),
            columns=tuple(str(column) for column in handle.df.columns),
        )
        for handle in session.datasets.values()
    ]


def tool_versions() -> dict[str, str]:
    """Environment/tool versions worth pinning to a run -- best-effort, never a hard dependency."""
    versions = {"python": sys.version.split()[0]}
    for module_name in ("pandas", "numpy", "scipy"):
        try:
            module = __import__(module_name)
            versions[module_name] = str(getattr(module, "__version__", "unknown"))
        except ImportError:
            continue
    return versions


def capture(
    *,
    session_id: str,
    message_id: int,
    instruction: str,
    answer: str,
    dataset_manifest: list[DatasetManifestEntry],
    steps: list[ExecutedStep],
    analysis: dict[str, Any],
    warnings: list[str],
) -> AnalysisRun:
    """Builds the immutable snapshot -- the one place `_finalize` constructs it from live state."""
    return AnalysisRun(
        session_id=session_id,
        message_id=message_id,
        instruction=instruction,
        answer=answer,
        dataset_manifest=tuple(dataset_manifest),
        steps=tuple(steps),
        analysis=analysis,
        warnings=tuple(warnings),
        tool_versions=tool_versions(),
        created_at=time.time(),
    )
