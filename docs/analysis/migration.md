# Migration Strategy

How the analytical control plane lands without destabilising the existing loop, session model, or
API surface.

## Additive persistence only

Every new table (`analysis_state`, `plan_revisions`, `evidence_nodes`, `evidence_edges`,
`analysis_runs`) is `CREATE TABLE IF NOT EXISTS` appended to the existing `SCHEMA` list in
[`database.py`](../../backend/src/core/database.py). No existing table's shape changes. A
database file created before this initiative opens unmodified; the new tables are created lazily
on first write, exactly like every existing table today.

## Additive events only

New `EventType` members (`PLAN_REVISED` already exists and was under-used; new ones such as
`CRITIC_FINDING`, `ROUTE_COMPARISON`, `CONFIDENCE`) are appended to the `EventType` enum. Existing
frame types, their payload shapes, and their emission order are unchanged. A client — including
the current frontend before Phase 13 — that ignores unknown frame types continues to work
unmodified, per the existing "a client that ignores these degrades to the previous experience"
principle already documented for the investigation frames.

## Additive `RunResult` fields only

`RunResult.to_dict()` gains new optional keys as each phase lands (`analysis`, `plan_revisions`,
`confidence`, ...). Existing keys keep their type and meaning. `backend/scripts/generate_openapi.py`
is re-run whenever a schema changes so `frontend/lib/api-types.generated.ts` stays in sync — this
is the existing workflow already documented in `backend/CLAUDE.md`, not a new one.

## Backward-compatible turn behaviour

Every phase's acceptance criteria requires the existing loop's *observable behaviour* (which
action runs when, what the answer looks like, what gets grounded) to be unchanged unless a phase
explicitly says otherwise (Phase 6 onward, where validators can add warnings; Phase 9, where a
turn can now terminate as `cannot_answer`). `test_regressions.py` and `test_turn_cost.py` are the
existing guardrails against an accidental behaviour or call-count change; they are run after every
phase, never edited to make a regression pass except where a phase's acceptance criteria
explicitly changes the pinned behaviour.

## Tier gating as the rollout mechanism

New analytical stages are gated the same way `allow_reflection`/`allow_verification`/
`allow_subagents` already gate existing ones: via `TierBudget`. This means the rollout is already
"safe by default" — a `compact`-tier deployment (the one most likely to be resource constrained)
sees no new model calls until the operator's host sizing lets `budget_for` resolve to a higher
tier. No feature flag rollout process is needed beyond the tier system that already exists.

## Session/state compatibility

`RunState.analysis: AnalyticalState` is a new field with a default factory; nothing reads it until
Phase 1 lands, and nothing before Phase 1 needs to construct it explicitly. `Investigation`'s
existing `findings`/`assumptions` lists remain the source of truth (see ADR-relevant note in
`state-model.md`), so no session in flight during a deploy can end up with two disagreeing copies.

## Order of operations for a safe rollout

1. Land phases 0–3 (state, plan, provenance) — purely additive, no behaviour change, verified by
   `test_regressions.py` passing unmodified.
2. Land phases 4–6 (understanding, methods, validation) — `_act_inspect` and `_verify` gain
   optional richer detail; existing outputs are supersets of before.
3. Land phases 7–9 (hypotheses/critic, competing analysis, confidence/stopping) — the first phases
   that can change a turn's *outcome* (a `cannot_answer` verdict is new). Each is tier-gated so a
   compact-tier deployment is unaffected until explicitly upgraded.
4. Land phases 10–12 (runs, benchmark, reporting) — consumption-side; nothing in the loop itself
   changes further.
5. Land phase 13 (frontend) once the backend event/schema surface is stable enough that a second
   `generate_openapi.py` pass is not expected.
6. Land phase 14 (perf) last, measured against the Phase 11 benchmark suite as the regression gate.
