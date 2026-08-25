"""The agent's structured beliefs about one turn's analysis -- PLAN.md's "analytical state".

Distinct from `agent.orchestrator.RunState`, which is per-turn transport/control-flow
bookkeeping the orchestrator already owns. `findings`/`assumptions` stay owned by
`agent.actions.Investigation` -- the single source of truth during a live turn -- and are
exposed here as properties so no existing call site changes; a state reloaded from storage
(no live `Investigation` attached) falls back to the snapshot captured at `to_dict()` time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.core.analysis.hypotheses import HypothesisSet
from src.core.analysis.objective import AnalyticalObjective
from src.core.analysis.plan import AnalyticalPlan
from src.core.analysis.provenance import EvidenceGraph


if TYPE_CHECKING:
    from src.core.agent.actions import Investigation


@dataclass
class AnalyticalState:
    """One turn's beliefs: objective, plan, understanding, hypotheses, evidence, confidence."""

    objective: AnalyticalObjective | None = None
    plan: AnalyticalPlan = field(default_factory=AnalyticalPlan)
    #: Populated by Phase 4's data-understanding engine; a plain dict until then.
    understanding: dict[str, Any] | None = None
    #: See core/analysis/hypotheses.py.
    hypotheses: HypothesisSet = field(default_factory=HypothesisSet)
    #: The provenance graph -- see core/analysis/provenance.py.
    evidence: EvidenceGraph = field(default_factory=EvidenceGraph)
    #: Node ids from `evidence` worth surfacing directly, in the order they became relevant.
    evidence_refs: list[str] = field(default_factory=list)
    #: Populated by Phase 6's validation framework.
    validations: list[dict[str, Any]] = field(default_factory=list)
    #: Populated by Phase 7's adversarial critic.
    critic_findings: list[dict[str, Any]] = field(default_factory=list)
    #: Populated by Phase 8's competing-methods comparison, one entry per `parallel` fan-out.
    route_comparisons: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    #: Populated by Phase 9's confidence rubric.
    confidence: dict[str, Any] | None = None
    #: Set by the orchestrator once the turn's Investigation exists; never serialised.
    investigation: Investigation | None = field(default=None, repr=False, compare=False)
    _findings_snapshot: list[str] = field(default_factory=list, repr=False)
    _assumptions_snapshot: list[str] = field(default_factory=list, repr=False)

    @property
    def findings(self) -> list[str]:
        return self.investigation.findings if self.investigation is not None else self._findings_snapshot

    @property
    def assumptions(self) -> list[str]:
        return self.investigation.assumptions if self.investigation is not None else self._assumptions_snapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.to_dict() if self.objective is not None else None,
            "plan": self.plan.to_dict(),
            "understanding": self.understanding,
            "hypotheses": self.hypotheses.to_dict(),
            "findings": self.findings,
            "assumptions": self.assumptions,
            "evidence": self.evidence.to_dict(),
            "evidence_refs": self.evidence_refs,
            "validations": self.validations,
            "critic_findings": self.critic_findings,
            "route_comparisons": self.route_comparisons,
            "open_questions": self.open_questions,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalyticalState:
        objective_data = data.get("objective")
        plan_data = data.get("plan")
        return cls(
            objective=AnalyticalObjective.from_dict(objective_data) if objective_data else None,
            plan=AnalyticalPlan.from_dict(plan_data) if plan_data else AnalyticalPlan(),
            understanding=data.get("understanding"),
            hypotheses=HypothesisSet.from_dict(data.get("hypotheses")),
            evidence=EvidenceGraph.from_dict(data.get("evidence") or {}),
            evidence_refs=list(data.get("evidence_refs") or []),
            validations=list(data.get("validations") or []),
            critic_findings=list(data.get("critic_findings") or []),
            route_comparisons=list(data.get("route_comparisons") or []),
            open_questions=list(data.get("open_questions") or []),
            confidence=data.get("confidence"),
            _findings_snapshot=list(data.get("findings") or []),
            _assumptions_snapshot=list(data.get("assumptions") or []),
        )
