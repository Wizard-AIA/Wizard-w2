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

File-backed dataset snapshots are stored with a run so an export can be rebuilt from the captured
inputs rather than the session's mutable current tables. Connector-backed inputs remain metadata
only and are explicitly reported as requiring a live connection at re-run time.
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
    origin: str = ""
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table_key": self.table_key,
            "content_hash": self.content_hash,
            "rows": self.rows,
            "columns": list(self.columns),
            "origin": self.origin,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetManifestEntry:
        return cls(
            name=str(data.get("name", "")),
            table_key=str(data.get("table_key", "")),
            content_hash=str(data.get("content_hash", "")),
            rows=int(data.get("rows", 0)),
            columns=tuple(data.get("columns") or ()),
            origin=str(data.get("origin", "")),
            target=str(data.get("target", "")),
        )


@dataclass(frozen=True)
class ExecutedStep:
    """One step's code and outcome, frozen at the moment it ran."""

    goal: str
    code: str
    ok: bool = True
    observation: str = ""
    duration_ms: int = 0
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "code": self.code,
            "ok": self.ok,
            "observation": self.observation,
            "duration_ms": self.duration_ms,
            "retries": self.retries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutedStep:
        return cls(
            goal=str(data.get("goal", "")),
            code=str(data.get("code", "")),
            ok=bool(data.get("ok", True)),
            observation=str(data.get("observation", "")),
            duration_ms=int(data.get("duration_ms", 0)),
            retries=int(data.get("retries", 0)),
        )


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
    dataset_files: dict[str, str] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    active_table_key: str = ""
    artifacts: tuple[dict[str, Any], ...] = ()
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
            "dataset_files": self.dataset_files,
            "telemetry": self.telemetry,
            "active_table_key": self.active_table_key,
            "artifacts": list(self.artifacts),
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
            dataset_files=dict(data.get("dataset_files") or {}),
            telemetry=dict(data.get("telemetry") or {}),
            active_table_key=str(data.get("active_table_key", "")),
            artifacts=tuple(data.get("artifacts") or ()),
            warnings=tuple(data.get("warnings") or ()),
            tool_versions=dict(data.get("tool_versions") or {}),
            created_at=float(data.get("created_at") or 0.0),
        )

    def changed_since(self, current_manifest: list[DatasetManifestEntry]) -> list[str]:
        """Table names whose captured identity no longer matches the current session."""
        current_by_name = {entry.name: entry for entry in current_manifest}
        return [
            entry.name
            for entry in self.dataset_manifest
            if entry.name not in current_by_name
            or current_by_name[entry.name].content_hash != entry.content_hash
            or current_by_name[entry.name].columns != entry.columns
            or current_by_name[entry.name].rows != entry.rows
            or current_by_name[entry.name].origin != entry.origin
            or current_by_name[entry.name].target != entry.target
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
            origin=handle.origin,
            target=str(handle.profile.get("target", "")),
        )
        for handle in session.datasets.values()
    ]


def dataset_files_from_session(session: Session) -> dict[str, str]:
    """Capture CSV inputs for file-backed tables, keyed by their bundle path."""
    return {
        f"data/{handle.table_key}.csv": handle.df.to_csv(index=False)
        for handle in session.datasets.values()
        if not handle.origin
    }


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
    dataset_files: dict[str, str] | None = None,
    telemetry: dict[str, Any] | None = None,
    active_table_key: str = "",
    artifacts: list[dict[str, Any]] | None = None,
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
        dataset_files=dict(dataset_files or {}),
        telemetry=dict(telemetry or {}),
        active_table_key=active_table_key,
        artifacts=tuple(artifacts or ()),
        warnings=tuple(warnings),
        tool_versions=tool_versions(),
        created_at=time.time(),
    )
