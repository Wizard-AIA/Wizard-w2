# ADR 0003: Eight validator categories, selectively invoked, not run unconditionally

## Status
Accepted.

## Context
PLAN.md §7 lists eight distinct validation concepts and explicitly warns against turning
verification into "one generic second calculation," and against running every validator on every
request. `_verify` today is exactly one of the eight (computational) and already respects tier
budgets and time budgets.

## Decision
`analysis/validation/base.py` defines a `Validator` protocol returning `Finding`s, plus a
`registry.py` that selects which validators apply given the objective's analytical type, the
method chosen, and the tier — mirroring `_allowed_actions`' existing menu-narrowing pattern rather
than inventing a new selection mechanism. `_verify`'s current logic becomes
`validation/computational.py`, unchanged in behaviour, registered as one entry.
`council.py`'s three specialists become `validation` adapters wrapping the existing
`SpecialistAgent` classes, so `COUNCIL_ENABLED` and existing tests keep working unmodified.

## Consequences
- New validator categories (data, semantic, statistical, sensitivity, alternative-method,
  consistency, reproducibility) are added independently in Phase 6, each its own file, each
  independently testable.
- Selection logic is one place (`registry.py`), so "why did validator X not run" has one answer.
- Existing `_verify` and `_review` call sites keep their signatures; the registry is invoked
  alongside them, not instead of them, until Phase 6 explicitly rewires them as adapters.
