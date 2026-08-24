# Architecture Gap Analysis — Analytical Control Plane

Audit performed against the repository at the start of the `feat/analytical-control-plane`
branch (commit `f271c3f`). This document is the Phase 0 deliverable required by the initiative's
implementation plan: a current-state assessment and target architecture detailed enough that
later phases do not re-derive it.

## 1. Current agent control loop

`AnalysisOrchestrator.run` in [`orchestrator.py`](../../backend/src/core/agent/orchestrator.py)
drives one turn: `orient` (plan) → optional plan-approval gate → `_investigate` (the
observe/decide/act loop) → `_verify` → `_review` → `_answer` → `_finalize`. This is already a
loop, not a fixed pipeline — each iteration re-decides from real output — and is the correct
foundation for "agent control" as defined in the target plan. Nothing about it needs replacing;
new phases add call sites inside it.

## 2. Current planning mechanism

`_orient` produces one prose plan per turn from `create_planning_prompt`. `_act_reflect`
(triggered by a `REFLECT` decision) regenerates the plan from `create_reflection_prompt` and
**overwrites** `state.plan` — no revision history survives. The plan is never a structured object;
every downstream consumer (decision prompt, answer prompt, UI) reads it as a string.

## 3. Current action space

`ActionKind` in [`actions.py`](../../backend/src/core/agent/actions.py): `INSPECT`, `CODE`,
`CONSULT`, `SEARCH` (gated, not selectable), `REFLECT`, `PARALLEL`, `ANSWER`. `parse_decision` is
deliberately fault-tolerant — malformed model output never raises, it degrades to a sensible
default. This robustness is worth preserving exactly as-is.

## 4. Current execution mechanism

`CodeExecutor.execute` ([`execution.py`](../../backend/src/core/execution.py)) is the sole path
from generated code to an interpreter, across three backends (`host`, `docker`, `inprocess`),
behind `CodeGuard`'s AST screening. This boundary is a hard invariant of the whole codebase and is
untouched by this initiative — new statistical helpers execute as ordinary generated code through
the same guard, not through a new path.

## 5. Current state representation

`RunState` (per-turn) and `Investigation` (per-turn working memory: `steps`, `findings`,
`assumptions`) are the entire state model today. There is no persisted, structured belief about
the dataset, no hypothesis object, no uncertainty representation. `Investigation` is the closest
existing analogue to "analytical state" and is extended, not replaced.

## 6. Current observation representation

`Step.render` / `Investigation.render` truncate and summarise prior output for the next prompt.
Deterministic and already tier-aware (`observation_chars` in `TierBudget`). Reused as-is by new
validators and the state model — no second summarisation path is introduced.

## 7. Current memory/retrieval mechanisms

`working_memory` (SQLite, [`memory.py`](../../backend/src/core/memory.py)) stores per-interaction
summaries for cross-turn context and the executive report. `semantic_cache` short-circuits
repeated questions. Neither carries provenance or structured evidence; Phase 3 adds that layer
alongside, not instead of, these.

## 8. Current verification mechanisms

`_verify` re-derives the headline result with independently generated code — a real second
execution, not a re-review. This is exactly "computational validation" from the target validation
taxonomy and becomes the first validator in the Phase 6 registry, unchanged in behaviour.

## 9. Current grounding mechanisms

[`grounding.py`](../../backend/src/core/agent/grounding.py): `check_grounding` verifies every
number in the answer traces to real execution output (exact, rounded, or scaled match) or to the
user's own question; `assumptions_from_code`/`assumptions_from_profile` read silent analytical
decisions (dropna, inner join, sampling, ...) directly out of the code and load profile. Both are
deterministic, report without rewriting, and are the base the provenance and confidence layers
build on rather than duplicate.

## 10. Current assumption extraction

Covered by `grounding.py` above — code-pattern matching against a fixed table
(`CODE_ASSUMPTIONS`) plus load-time profile flags (truncation, dropped/renamed columns). Literal
by design: reports what code demonstrably did, not what a model claims.

## 11. Current subagent architecture

`_act_parallel` / `SubagentSession` / `BranchEmitter` fan a `parallel` decision into isolated
per-branch sessions sharing the parent's data and permissions, serial in `inprocess`, concurrent
via `asyncio.gather` on real backends. Phase 8's competing-analysis routes reuse this machinery
for "run method A and method B" rather than inventing a second fan-out mechanism.

## 12. Current permissions/security model

Three axes — data mode (`local-only`/`cloud-only`/`hybrid`), permission profile
(`auto-approve`/`ask-always`/`custom`), and category rulings (`library_install`, `network`,
`workspace_write`, `db_connect`, `db_write`) — enforced at `LLMProvider.resolve` and
`ConsentBroker`. Fully preserved; no new capability bypasses it.

## 13. Current persistence/provenance model

SQLite via `db_mgr` ([`database.py`](../../backend/src/core/database.py)):
`semantic_cache`, `trajectories`, `feedbacks`, `working_memory`, `schema_registry`,
`skill_candidates`, `skill_usage`, `chat_messages`. No table today records *why* a claim was
believed — only that a turn happened. Phase 3 adds provenance tables additively.

## 14. Current export/reporting model

`agent/export.py` builds a runnable script/notebook from the turn's real executed steps (never
from a description of them); `reporting.py` renders an executive summary from `working_memory`
rows. Neither is versioned against a specific analysis run; a later turn's workspace write
silently invalidates an earlier turn's on-disk `analysis.py`. Phase 10 fixes this by rendering
from an immutable run snapshot instead of live/mutable state.

## 15. Current benchmark/evaluation model

`scripts/benchmark_harness/`: content-based grading (`grading.py`) that never trusts a
self-reported status field — the exact defect the harness was built to stop repeating — plus a
live-inference harness (`run_benchmark.py`) not run in this session (no live-model constraint).
Ten adversarial scenario types from the target plan are not yet represented; Phase 11 adds them
as offline, stub-driven fixtures so they can run in CI.

## 16. Existing technical debt relevant to the target architecture

- `orchestrator.py` is already 2247 lines; every phase must add call sites into it, not more
  inline logic, or it becomes unmaintainable.
- `stats.py` has three functions (normality, outliers, correlation) — far short of the
  methodology layer's requirements; Phase 5 expands it in place.
- `council.py`'s three specialists overlap conceptually with the target validation taxonomy and
  should become an adapter into the Phase 6 registry rather than a parallel system.

## Subsystems explicitly NOT rewritten

Sandbox/OS containment, `CodeGuard`, connectors, skills, credentials, data mode enforcement,
the WebSocket/REST transport layer, and the core loop shape. See the "must not be rewritten"
table in the plan for the full list with rationale.
