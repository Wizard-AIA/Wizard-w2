"""Structured, text-free telemetry for one agent turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TerminationReason = Literal["completed", "failed", "cancelled", "awaiting_plan", "escalated"]


@dataclass
class TurnTrace:
    turn_id: str
    intent: str = ""
    mode: str = "auto"
    workflow: str = ""
    complexity: str = ""
    plan_policy: str = "none"
    planner_used: bool = False
    escalated: bool = False
    models_used: list[str] = field(default_factory=list)
    tools_actions_invoked: list[str] = field(default_factory=list)
    generation_budgets: dict[str, int] = field(default_factory=dict)
    context_chars: int = 0
    output_tokens: int = 0
    llm_call_count: int = 0
    latency_ms: int = 0
    termination_reason: TerminationReason = "completed"

    @property
    def actions_invoked(self) -> list[str]:
        """Compatibility spelling for callers that do not use the combined field."""
        return self.tools_actions_invoked

    def to_log(self) -> dict[str, Any]:
        """Return a flat structured record containing no prompt/message text."""
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
            "tools_actions_invoked": list(self.tools_actions_invoked),
            "generation_budgets": dict(self.generation_budgets),
            "context_chars": self.context_chars,
            "output_tokens": self.output_tokens,
            "llm_call_count": self.llm_call_count,
            "latency_ms": self.latency_ms,
            "termination_reason": self.termination_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_log()
