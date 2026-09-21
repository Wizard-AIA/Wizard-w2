"""One structured, text-free record per agent turn.

The record answers "what did Wizard decide to do for this message, and what did
it cost": the route, which models ran, how many model calls, what each was
allowed to produce. It carries no message text, no prompt text and no
credentials, so it is safe to write to the log at info level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TerminationReason = Literal["completed", "failed", "cancelled", "awaiting_approval"]


@dataclass
class TurnTrace:
    turn_id: str
    intent: str = ""
    mode: str = "auto"
    workflow: str = ""
    complexity: str = ""
    plan_policy: str = "none"
    planner_used: bool = False
    #: A conversational reply that turned out to need the data and was rerouted.
    escalated: bool = False
    models_used: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    #: Output-token allowance per purpose, as resolved for this turn.
    generation_budgets: dict[str, int] = field(default_factory=dict)
    #: Characters of prompt sent to a model, summed over the turn.
    context_chars: int = 0
    llm_call_count: int = 0
    latency_ms: int = 0
    termination_reason: TerminationReason = "completed"

    def observe_route(self, route: Any) -> None:
        """Copies what routing decided. Called once per route, so an escalation overwrites."""
        self.intent = route.intent.value
        self.workflow = route.workflow.value
        self.complexity = route.complexity.value
        self.plan_policy = route.plan.value

    def record_call(self, purpose: str, budget: int, prompt_chars: int, model: str) -> None:
        self.llm_call_count += 1
        self.context_chars += prompt_chars
        # The largest allowance a purpose was given this turn; a purpose is
        # called more than once, and the loop's later calls are the widest.
        self.generation_budgets[purpose] = max(budget, self.generation_budgets.get(purpose, 0))
        if purpose == "plan":
            self.planner_used = True
        if model and model not in self.models_used:
            self.models_used.append(model)

    def to_log(self) -> dict[str, Any]:
        """A flat structured record. Contains no prompt or message text."""
        return {
            "turn_id": self.turn_id,
            "intent": self.intent,
            "mode": self.mode,
            "workflow": self.workflow,
            "complexity": self.complexity,
            "plan_policy": self.plan_policy,
            "planner_used": self.planner_used,
            "escalated": self.escalated,
            "models_used": list(self.models_used),
            "actions": list(self.actions),
            "generation_budgets": dict(self.generation_budgets),
            "context_chars": self.context_chars,
            "llm_call_count": self.llm_call_count,
            "latency_ms": self.latency_ms,
            "termination_reason": self.termination_reason,
        }
