"""Reproducibility validation: randomness used without a fixed seed.

A sample, shuffle or bootstrap that is not seeded will not reproduce -- a different reviewer, or
the same one re-running the notebook later, gets a different number for "the same" analysis.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


_RANDOM_MARKERS = (".sample(", "np.random.", "random.", "shuffle(")
_SEED_MARKERS = ("random_state=", "seed(", "default_rng(")


class ReproducibilityValidator:
    name = "reproducibility"

    def applicable(self, ctx: ValidationContext) -> bool:
        return any(marker in ctx.code for marker in _RANDOM_MARKERS)

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        if any(marker in ctx.code for marker in _SEED_MARKERS):
            return []
        message = "The code uses randomness without a fixed seed; results will not be exactly reproducible."
        return [Finding(self.name, "warning", message)]
