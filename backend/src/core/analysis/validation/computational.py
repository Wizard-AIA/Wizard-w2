"""Wraps `orchestrator._verify`'s independent recomputation as a validator.

The recomputation itself -- a second LLM call, a second execution, consent-gated installs -- stays
in the orchestrator; it is loop machinery, not a judgement over already-produced evidence, and the
existing tests pin its exact call count. This validator only turns the status it already computed
into a `Finding`, so it costs nothing extra and its behaviour is exactly `_verify`'s prior inline
logic, just named and reusable.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


class ComputationalValidator:
    name = "computational"

    def applicable(self, ctx: ValidationContext) -> bool:
        return ctx.recomputation_status is not None

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        status, detail = ctx.recomputation_status, ctx.recomputation_detail[:500]
        if status == "mismatch":
            return [Finding(self.name, "error", "Independent recomputation disagreed with the analysis.", detail)]
        if status == "verified":
            return [Finding(self.name, "info", "Independent recomputation matched the analysis.", detail)]
        return [Finding(self.name, "warning", "Independent recomputation was inconclusive.", detail)]
