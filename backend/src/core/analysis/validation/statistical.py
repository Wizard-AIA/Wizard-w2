"""Statistical validation: claims made without the numbers to back them.

The deterministic half of `council.StatisticianAgent`'s original check, pulled out here so
`council.py` and the validator registry share one implementation; the LLM-generated caveat
`StatisticianAgent` adds afterwards stays council-only, since escalation-on-demand is Phase 7's
critic concern, not this framework's.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


_RELEVANT = ("test", "hypothesis", "significan", "correlat", "regress", "model", "predict", "distribution")


def check_significance_claims(plan: str, code: str, output: str) -> list[Finding]:
    haystack = f"{plan} {code}".lower()
    if not any(marker in haystack for marker in _RELEVANT):
        return []

    findings: list[Finding] = []
    lowered = output.lower()
    claims_significance = "significan" in lowered or "reject" in lowered
    reports_p_value = "p-value" in lowered or "p_value" in lowered or "pvalue" in lowered
    if claims_significance and not reports_p_value:
        findings.append(Finding("statistical", "warning", "Significance is claimed but no p-value is reported."))

    if "corr" in haystack and "causal" in lowered:
        findings.append(Finding("statistical", "warning", "Correlation is being described in causal terms."))

    return findings


class StatisticalValidator:
    name = "statistical"

    def applicable(self, ctx: ValidationContext) -> bool:
        return any(marker in f"{ctx.plan} {ctx.code}".lower() for marker in _RELEVANT)

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        return check_significance_claims(ctx.plan, ctx.code, ctx.output)
