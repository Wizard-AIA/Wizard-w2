"""Selective validator invocation -- PLAN.md Layer 4.

Eight validators exist; no turn runs all eight. Which ones a turn affords is the tier's call
(mirroring `TierBudget`'s existing gating of verification itself); which of those actually have
anything to say about this turn is each validator's own `applicable()`.
"""

from __future__ import annotations

from src.core.analysis.validation.alternative import AlternativeValidator
from src.core.analysis.validation.base import Finding, ValidationContext, Validator
from src.core.analysis.validation.computational import ComputationalValidator
from src.core.analysis.validation.consistency import ConsistencyValidator
from src.core.analysis.validation.data import DataValidator
from src.core.analysis.validation.reproducibility import ReproducibilityValidator
from src.core.analysis.validation.semantic import SemanticValidator
from src.core.analysis.validation.sensitivity import SensitivityValidator
from src.core.analysis.validation.statistical import StatisticalValidator


ALL_VALIDATORS: dict[str, Validator] = {
    validator.name: validator
    for validator in (
        ComputationalValidator(),
        DataValidator(),
        SemanticValidator(),
        StatisticalValidator(),
        SensitivityValidator(),
        AlternativeValidator(),
        ConsistencyValidator(),
        ReproducibilityValidator(),
    )
}

#: What each tier is willing to pay for. Compact tier is not listed: `orchestrator._verify`
#: returns before this registry is ever reached on that tier, the same gate that already turns
#: verification itself off below balanced -- so this module adds no cost there at all.
TIER_VALIDATORS: dict[str, tuple[str, ...]] = {
    "balanced": ("computational", "data", "statistical", "consistency", "reproducibility"),
    "full": tuple(ALL_VALIDATORS),
}


def run_validators(ctx: ValidationContext, tier: str) -> list[Finding]:
    """Runs every validator this tier affords and this turn's evidence makes applicable."""
    names = TIER_VALIDATORS.get(tier, TIER_VALIDATORS["balanced"])
    findings: list[Finding] = []
    for name in names:
        validator = ALL_VALIDATORS[name]
        if validator.applicable(ctx):
            findings.extend(validator.validate(ctx))
    return findings


def cache_key(*, tier: str, code: str, content_hash: str, method: str, recomputation_status: str) -> str:
    """A stable key for one `run_validators` call -- Phase 14: every validator here is a pure
    function of `ValidationContext`, so the exact same (tier, code, dataset, method, verification
    outcome) combination is guaranteed to produce the exact same findings, and a cache hit can
    skip re-running all eight without approximating anything.
    """
    return f"{tier}::{content_hash}::{method}::{recomputation_status}::{code}"
