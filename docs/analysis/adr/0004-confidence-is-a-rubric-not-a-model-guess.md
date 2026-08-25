# ADR 0004: Confidence is computed from named components, never an LLM-emitted percentage

## Status
Accepted.

## Context
PLAN.md Rule 5 ("never fabricate statistical confidence") and §12 both require confidence to be
explainable and componentised, not a bare number a language model invents.

## Decision
`analysis/confidence.py` computes confidence from concrete, independently-derived components:
- data completeness (`CatalogEngine`'s existing `completeness_score`)
- sample size (row count against a documented floor per analytical type)
- method robustness (from the `analysis/methods.py` registry entry's own assumption checks)
- verification status (the computational validator's `verified`/`mismatch`/`inconclusive`)
- method agreement (Phase 8's competing-route comparison, when run)
- sensitivity (Phase 6's sensitivity validator, when run)
- unresolved assumptions (count of open items in `AnalyticalState.assumptions`/`open_questions`)
- model uncertainty (whether the manager's own decision was `inferred=True` this turn — the
  existing `Decision.inferred` flag already tracks exactly this)

Each component maps to a fixed ordinal scale (`low`/`medium`/`high`) via a documented, tested
rubric — not a prompt asking a model to self-assess. The overall verdict is a deterministic
function of the components (e.g. any critical component at `low` caps the overall verdict), never
an average a model computes in its head.

## Consequences
- The rubric is code, testable and mutation-tested, not prompt text that drifts across models.
- A `low` overall confidence is always traceable to a specific named component.
- Cost: this requires the components to actually exist (verification, understanding, etc.) —
  confidence available only expands as later phases land; earlier phases report fewer components
  honestly rather than interpolating missing ones.
