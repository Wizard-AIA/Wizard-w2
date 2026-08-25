"""The shared evidence/run model every report renderer reads from -- PLAN.md Layer 9 / Phase 12.

Built once from an immutable `AnalysisRun`, never from live state (ADR 0005's "never a report
that silently refers to state modified by a later analysis"). The three renderers in this package
differ only in which of these facts they surface and how much narrative they wrap around them --
none of them derives a fact `ReportModel` does not already carry, and none of them calls an LLM:
every figure in an answer is already a `claim` node in the turn's own evidence graph (see
`agent.orchestrator._record_claim_evidence`), so a renderer only has to read the chain, never
invent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.analysis.provenance import EvidenceGraph
from src.core.analysis.runs import AnalysisRun


@dataclass
class Claim:
    """One grounded figure from the answer, with the chain of evidence nodes behind it."""

    value: str
    #: Node ids the claim traces to, roots first, the claim itself last -- from
    #: `EvidenceGraph.trace`. A length of 1 means the claim itself is the only node found (no
    #: execution recorded it), reported as-is rather than padded into a false chain.
    provenance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "provenance": self.provenance}


@dataclass
class ReportModel:
    """Everything a renderer might cite, gathered once from one `AnalysisRun`."""

    session_id: str
    message_id: int
    instruction: str
    answer: str
    created_at: float
    objective: dict[str, Any] | None
    understanding: dict[str, Any] | None
    plan_revisions: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    validations: list[dict[str, Any]]
    critic_findings: list[dict[str, Any]]
    route_comparisons: list[dict[str, Any]]
    confidence: dict[str, Any] | None
    warnings: list[str]
    dataset_manifest: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    tool_versions: dict[str, str]
    claims: list[Claim]

    @property
    def every_claim_has_provenance(self) -> bool:
        """Whether every claim traces back to at least the execution that produced it -- the
        acceptance property PLAN.md's Phase 12 asks a report never violate."""
        return all(len(claim.provenance) > 1 for claim in self.claims)


def build(run: AnalysisRun) -> ReportModel:
    """The one place a `ReportModel` is assembled -- every renderer calls this, never its own
    reading of `run.analysis`, so the three modes cannot silently diverge on the facts."""
    analysis = run.analysis or {}
    graph = EvidenceGraph.from_dict(analysis.get("evidence") or {})
    claims = [
        Claim(value=node.label, provenance=[n.id for n in graph.trace(node.id)])
        for node in graph.nodes.values()
        if node.kind == "claim"
    ]
    return ReportModel(
        session_id=run.session_id,
        message_id=run.message_id,
        instruction=run.instruction,
        answer=run.answer,
        created_at=run.created_at,
        objective=analysis.get("objective"),
        understanding=analysis.get("understanding"),
        plan_revisions=list((analysis.get("plan") or {}).get("revisions") or []),
        hypotheses=list(analysis.get("hypotheses") or []),
        validations=list(analysis.get("validations") or []),
        critic_findings=list(analysis.get("critic_findings") or []),
        route_comparisons=list(analysis.get("route_comparisons") or []),
        confidence=analysis.get("confidence"),
        warnings=list(run.warnings),
        dataset_manifest=[entry.to_dict() for entry in run.dataset_manifest],
        steps=[step.to_dict() for step in run.steps],
        tool_versions=dict(run.tool_versions),
        claims=claims,
    )
