"""Hypothesis management -- PLAN.md Layer 5.

A hypothesis is a specific, falsifiable claim the investigation is testing -- distinct from the
objective (what was asked) and a finding (something observed along the way). Evidence for and
against is tracked by evidence-graph node id, not duplicated text, so a hypothesis's support is
exactly the claims and validations `analysis.provenance.EvidenceGraph` already recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Kind = Literal["primary", "null", "alternative", "exploratory", "competing"]
Status = Literal["untested", "supported", "refuted", "inconclusive", "unresolved"]


@dataclass
class Hypothesis:
    """One specific, falsifiable claim -- what it says, and what has been checked against it."""

    id: str
    kind: Kind
    statement: str
    status: Status = "untested"
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "statement": self.statement,
            "status": self.status,
            "evidence_for": self.evidence_for,
            "evidence_against": self.evidence_against,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        return cls(
            id=str(data.get("id", "")),
            kind=data.get("kind", "exploratory"),
            statement=str(data.get("statement", "")),
            status=data.get("status", "untested"),
            evidence_for=list(data.get("evidence_for") or []),
            evidence_against=list(data.get("evidence_against") or []),
        )


def _index_of(hyp_id: str) -> int:
    try:
        return int(hyp_id.rsplit("-", 1)[-1])
    except ValueError:
        return -1


@dataclass
class HypothesisSet:
    """Every hypothesis considered this turn. Ids are per-kind sequence numbers, mirroring
    `EvidenceGraph`'s node ids, so a hypothesis can be cited (`"primary-0"`) as plainly as an
    evidence node can."""

    items: dict[str, Hypothesis] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    def add(self, kind: Kind, statement: str) -> str:
        index = self._counters.get(kind, 0)
        self._counters[kind] = index + 1
        hyp_id = f"{kind}-{index}"
        self.items[hyp_id] = Hypothesis(id=hyp_id, kind=kind, statement=statement)
        return hyp_id

    def get(self, hyp_id: str) -> Hypothesis | None:
        return self.items.get(hyp_id)

    def record_evidence(self, hyp_id: str, node_id: str, *, supports: bool) -> None:
        """Links an evidence-graph node id as for or against a hypothesis."""
        hypothesis = self.items.get(hyp_id)
        if hypothesis is None:
            return
        target = hypothesis.evidence_for if supports else hypothesis.evidence_against
        if node_id not in target:
            target.append(node_id)

    def set_status(self, hyp_id: str, status: Status) -> None:
        hypothesis = self.items.get(hyp_id)
        if hypothesis is not None:
            hypothesis.status = status

    def to_dict(self) -> list[dict[str, Any]]:
        return [hypothesis.to_dict() for hypothesis in self.items.values()]

    @classmethod
    def from_dict(cls, data: list[dict[str, Any]] | None) -> HypothesisSet:
        result = cls()
        for entry in data or []:
            hypothesis = Hypothesis.from_dict(entry)
            result.items[hypothesis.id] = hypothesis
            result._counters[hypothesis.kind] = max(
                result._counters.get(hypothesis.kind, 0), _index_of(hypothesis.id) + 1
            )
        return result
