# ADR 0001: Introduce an Analytical Control Plane alongside the existing loop

## Status
Accepted.

## Context
The existing `AnalysisOrchestrator` loop (agent control) already separates "what to do next" from
"what actually happened," but has no place to keep structured beliefs about the analysis
(analytical state), no traceability from a claim back to its computation (evidence/provenance),
and no distinct governance layer beyond permissions/data-mode (which already exist and are kept
as-is). PLAN.md §3 asks for these four concepts to stay conceptually separate rather than being
collapsed into one giant prompt.

## Decision
Add a new package, `backend/src/core/analysis/`, that the orchestrator imports and calls into, and
that never imports back from `orchestrator.py`. Each of the four concepts gets its own module
family (state, plan, provenance; validation/governance stays in existing `permissions.py` /
`data_mode.py`). The orchestrator's loop shape, action space, and event protocol are extended with
new call sites and new event types, not restructured.

## Consequences
- The 2247-line orchestrator grows by call sites, not by inline logic.
- `core/analysis/` is independently testable without spinning up a `Session`/orchestrator turn.
- A phase can be reverted by reverting its commit without touching the loop's control flow.
- Risk: a future contributor could be tempted to put control-flow logic in `core/analysis/` to
  avoid touching the orchestrator. Mitigated by review — `core/analysis/` modules return data,
  they do not call the LLM provider or the emitter directly except where a validator explicitly
  needs to (mirroring `council.py`'s existing pattern).
