# Benchmark Strategy

Extends the existing `scripts/benchmark_harness/` (content-based grading, never a self-reported
status field — see its own README for the defect that established that rule) to cover agentic
analytical quality per PLAN.md §18, not just per-case factual/computational correctness.

## What already exists and is reused

- `grading.py`'s number-matching (`grounding.check_grounding`'s own tolerance logic) — no second
  "close enough" definition is introduced.
- `run_benchmark.py`'s live-orchestrator harness, retry counting via the `STATUS` event stream,
  and host-precondition capture — reused for live-model confirmation runs, left for the user to
  execute (standing "no live model workloads in-session" constraint).
- `EventCollector` from `agent/events.py` as the observation surface — benchmarks read the same
  event stream the frontend would, never internal orchestrator state directly.

## New in Phase 11

### Offline adversarial scenario fixtures

Ten scenario types from PLAN.md §18, each a small synthetic dataset plus a question plus a
scripted stub LLM response sequence (reusing `tests/stubs.py`'s `ScriptedLLM`), so the full suite
runs in CI with no model server and no network:

1. Obvious analysis is wrong (a naive aggregation misses a segment that flips the conclusion)
2. Dirty join key (inconsistent casing/whitespace across tables)
3. Target variable contains leakage (a column that encodes the outcome)
4. Missing data changes the conclusion (dropna silently excludes a decisive subgroup)
5. Two valid methods disagree (parametric vs. non-parametric test give different verdicts)
6. Misleading aggregation creates Simpson's paradox
7. Requested analysis is statistically inappropriate (e.g. a t-test on non-independent samples)
8. Dataset does not contain enough evidence to answer
9. Question is ambiguous (multiple defensible readings)
10. Answer requires multi-step investigation (no single query suffices)

### Metric scorers (extend `grading.py`)

Beyond the existing correctness check: **provenance completeness** (every grounded number in the
answer has a resolvable evidence-graph trace, Phase 3), **appropriate uncertainty** (a scenario
built to be ambiguous or insufficient should not produce an unqualified `answerable` verdict),
**correct refusal** (scenarios 3, 8, 9 specifically reward a `cannot_answer` or
`insufficient_evidence` verdict — refusal is scored as a pass, not a failure, exactly per
PLAN.md's "the benchmark should reward correct refusal"), **unnecessary tool calls** and
**premature/failed stopping** (both read from `iterations_used` and the action sequence in the
event stream), **efficiency/cost** (reuses `usage_ledger` totals already surfaced on `RunResult`).

### Location and CI wiring

`backend/tests/benchmark/` (new), pytest-collected, running against `ScriptedLLM` — no
`requires_llm`/`requires_docker` marker needed, so it runs in the standard CI job. The manual,
real-model comparison stays exactly where it is
(`scripts/benchmark_harness/run_benchmark.py`), untouched.

## Regression discipline

Per PLAN.md §11 ("Run regression benchmarks on every major agent-loop change"): the offline suite
runs in the standard `pytest` invocation from Phase 11 onward, so any later phase that changes
loop behaviour gets an immediate pass/fail signal from the adversarial fixtures, not just from
`test_regressions.py`'s narrower defect-pinning tests.
