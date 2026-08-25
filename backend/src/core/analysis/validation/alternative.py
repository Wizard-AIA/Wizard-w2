"""Alternative-method validation: notes when a competing method exists.

Only the awareness -- naming the alternative `analysis.methods` would suggest -- runs here.
Actually running that alternative and comparing results is Phase 8's competing-analysis work;
this is deliberately the cheap half, gated on a named method the way `understanding.py`'s checks
are gated on an explicit target rather than a guess.
"""

from __future__ import annotations

from src.core.analysis.methods import REGISTRY
from src.core.analysis.validation.base import Finding, ValidationContext


class AlternativeValidator:
    name = "alternative"

    def applicable(self, ctx: ValidationContext) -> bool:
        return bool(ctx.method) and ctx.method in REGISTRY

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        spec = REGISTRY.get(ctx.method or "")
        if spec is None or not spec.alternative:
            return []
        message = f"An alternative method ('{spec.alternative}') exists for this data; consider whether it agrees."
        return [Finding(self.name, "info", message, spec.alternative)]
