"""Model-tier selection for manager turns.

`TaskTier` says how much model a turn justifies. The decision is made by turn
routing (`core/agent/routing.py`), which reads whole tokens and session context;
this module keeps the enum and the historical `classify_task_complexity` entry
point as a thin wrapper over it. It used to hold its own substring tables, which
matched `hi` inside `which` and called any message under 15 characters chitchat.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class TaskTier(StrEnum):
    LIGHTWEIGHT = "lightweight"
    STANDARD = "standard"
    REASONING_HEAVY = "reasoning_heavy"


def classify_task_complexity(instruction: str, context: dict[str, Any] | None = None) -> TaskTier:
    """Classify a request as lightweight, standard, or reasoning-heavy.

    ``context`` is advisory: a previous failed attempt raises the tier, and a
    multi-step or document-backed request never drops below standard. An unknown
    request stays at the standard tier, so ambiguous work never silently loses the
    reasoning capacity reserved for investigation.
    """
    # Imported here: routing imports `TaskTier` from this module.
    from src.core.agent.routing import TurnContext, route_turn

    meta = context or {}
    tier = route_turn(instruction, TurnContext(has_dataset=True)).task_tier
    if meta.get("previous_error"):
        return TaskTier.REASONING_HEAVY
    if (meta.get("multi_step") or meta.get("has_documents")) and tier is TaskTier.LIGHTWEIGHT:
        return TaskTier.STANDARD
    return tier


__all__ = ["TaskTier", "classify_task_complexity"]
