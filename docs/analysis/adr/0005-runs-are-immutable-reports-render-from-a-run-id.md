# ADR 0005: Analysis runs are immutable; reports and reproducibility bundles render from a run id

## Status
Accepted.

## Context
`analysis.py` in the session workspace is silently overwritten by the next turn
(`orchestrator._write_script`), and `reporting.py` reads live `working_memory` rows. PLAN.md §15
states plainly: "The user must never receive a report that silently refers to state modified by a
later analysis."

## Decision
`analysis/runs.py` defines `AnalysisRun`, built once at `_finalize` time from everything the turn
actually produced (plan + revisions, executions, evidence, validations, warnings, environment
versions, dataset content hashes) and persisted to a new `analysis_runs` table, keyed by an
immutable id. `export.py`'s existing script/notebook builders and the Phase 12 report renderers
both take a run id (or an in-memory `AnalysisRun`) as their input, never `session` + "whatever is
currently in the workspace."

## Consequences
- A report generated after turn N+1 for turn N's run is unaffected by turn N+1.
- Storage grows per turn; mitigated by the existing `prune_memories`-style retention pattern,
  extended to `analysis_runs` in Phase 10 rather than left unbounded.
- `routes/export.py` gains a run-scoped variant; the existing message-id-scoped export
  (`GET /api/export/{message_id}`) keeps working by resolving message id → run id internally.
