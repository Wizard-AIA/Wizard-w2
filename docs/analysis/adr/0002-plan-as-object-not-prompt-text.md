# ADR 0002: The analytical plan is a structured object with a derived string, not a string

## Status
Accepted.

## Context
`state.plan` is prose today; `_act_reflect` overwrites it, discarding history. PLAN.md §5 requires
plans to be mutable with preserved revisions, driven by "observation → plan update," not just
"plan → execution."

## Decision
`analysis/plan.py` defines `AnalyticalPlan` + `PlanRevision`. `_orient` deterministically parses
the model's prose plan into the structured object (best-effort, tolerant of failure per the
existing `parse_decision` philosophy — a plan that cannot be parsed structurally still has its raw
text preserved and used exactly as `state.plan` is today). `_act_reflect` appends a new
`PlanRevision` rather than replacing `state.plan` outright; `state.plan` becomes the *current*
revision's rendered text, so every existing prompt call site (`create_decision_prompt`,
`create_answer_prompt`, etc.) is unaffected.

## Consequences
- Revision history is queryable after the turn (`plan_revisions` table) and exportable in the
  reproducibility bundle (Phase 10).
- No prompt template changes in this phase; `state.plan` keeps its existing type and meaning.
- The parser must never raise — mirrors `parse_decision`'s "malformed input degrades, does not
  fail the turn" rule.
