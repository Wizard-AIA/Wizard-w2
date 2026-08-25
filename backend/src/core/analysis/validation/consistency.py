"""Consistency validation: a mixed-grain table aggregated without grouping or deduplication.

Mixed grain means the same key recurs; summing straight over it silently double-counts. This is
one specific, checkable instance of that risk, not a general internal-consistency check.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


_DEDUPE_MARKERS = ("groupby", "drop_duplicates")


class ConsistencyValidator:
    name = "consistency"

    def applicable(self, ctx: ValidationContext) -> bool:
        return bool(((ctx.understanding or {}).get("grain") or {}).get("mixed"))

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        if any(marker in ctx.code for marker in _DEDUPE_MARKERS):
            return []
        grain = (ctx.understanding or {})["grain"]
        columns = ", ".join(grain.get("columns") or []) or "an unnamed column"
        message = (
            f"The data's grain is mixed on {columns}, but the code does not appear to group or "
            "deduplicate before aggregating."
        )
        return [Finding(self.name, "warning", message, str(grain))]
