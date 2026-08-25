"""Deterministic task routing for manager turns.

The router deliberately does not call a model. It classifies the user's request
from stable lexical signals so lightweight metadata questions can use a small
installed model without making model selection itself another LLM round-trip.
Unknown requests stay at the standard tier; ambiguous work must never silently
lose the reasoning capacity reserved for investigation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class TaskTier(StrEnum):
    LIGHTWEIGHT = "lightweight"
    STANDARD = "standard"
    REASONING_HEAVY = "reasoning_heavy"


_LIGHTWEIGHT_PHRASES = (
    "show first",
    "show top",
    "show head",
    "show last",
    "show tail",
    "preview",
    "list columns",
    "show columns",
    "column names",
    "schema",
    "data types",
    "column descriptions",
    "how many rows",
    "number of rows",
    "row count",
    "how big",
    "dataset dimensions",
    "hello",
    "hi",
    "thanks",
)

_STANDARD_MARKERS = (
    "calculate",
    "count",
    "mean",
    "average",
    "median",
    "sum",
    "describe",
    "statistics",
    "group by",
    "groupby",
    "join",
    "merge",
    "query",
    "sql",
    "plot",
    "chart",
    "clean",
    "filter",
    "transform",
    "format",
)

_REASONING_MARKERS = (
    "investigate",
    "anomal",
    "outlier",
    "hypothesis",
    "validate",
    "why",
    "explain",
    "root cause",
    "forecast",
    "predict",
    "regression",
    "causal",
    "compare",
    "trade-off",
    "tradeoff",
    "multi-step",
    "step by step",
    "deep analysis",
)


def classify_task_complexity(instruction: str, context: dict[str, Any] | None = None) -> TaskTier:
    """Classify a request as lightweight, standard, or reasoning-heavy.

    ``context`` is intentionally advisory. A long instruction, a previous
    failed attempt, or a multi-table session increases the required tier, while
    an explicit lightweight metadata request can still remain cheap.
    """
    text = " ".join((instruction or "").lower().split())
    metadata = context or {}

    if any(marker in text for marker in _REASONING_MARKERS):
        return TaskTier.REASONING_HEAVY

    if any(phrase in text for phrase in _LIGHTWEIGHT_PHRASES):
        if not metadata.get("previous_error") and not metadata.get("multi_step") and len(text) < 180:
            return TaskTier.LIGHTWEIGHT

    if metadata.get("previous_error") or metadata.get("multi_step") or metadata.get("has_documents"):
        return TaskTier.REASONING_HEAVY if metadata.get("previous_error") else TaskTier.STANDARD

    if any(marker in text for marker in _STANDARD_MARKERS) or len(text) > 180:
        return TaskTier.STANDARD

    # Conversational turns and unknown analytical requests should retain the
    # normal model until stronger evidence says they are safe to downscale.
    return TaskTier.STANDARD


__all__ = ["TaskTier", "classify_task_complexity"]
