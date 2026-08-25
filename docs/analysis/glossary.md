# Glossary — Analytical Control Plane

Terms as used across `docs/analysis/*` and the `core/analysis/` package. Where a term already has
a meaning elsewhere in the codebase (e.g. `Session`, `Investigation`), this glossary points at it
rather than redefining it.

**Analytical objective** — The structured form of the user's question: analytical type, unit of
analysis, population, time dimension, likely variables, constraints, and explicit ambiguity.
`analysis/objective.py`.

**Analytical state** — The agent's current beliefs about the analysis in progress: objective,
data understanding, hypotheses, assumptions, findings, evidence references, validations, open
questions, confidence. Persisted per turn. `analysis/state.py`. Distinct from `RunState`, which
is per-turn transport/control-flow bookkeeping the orchestrator already owns.

**Analytical plan** — A structured, revisable object (not prose) carrying objective, hypotheses,
intended analyses, required evidence, assumptions, dependencies, expected outputs, validation
criteria, stop criteria, fallbacks, and open uncertainties. `analysis/plan.py`. `state.plan`
(the rendered string used by existing prompts) is derived from it, not replaced by it.

**Plan revision** — One recorded change to the plan, keyed to what was observed that prompted it.
Plans accumulate revisions; they are never silently overwritten.

**Evidence graph / provenance graph** — The traceability structure: claim → result → computation
→ execution → dataset version → transformation lineage. `analysis/provenance.py`.

**Claim** — A specific, checkable statement in a final answer, generally centred on one grounded
number (see `grounding.check_grounding`). The unit the evidence graph traces.

**Dataset version** — An immutable identity for one state of a table, keyed by content hash. A
transformation produces a new dataset version, never mutates one in place.

**Validator** — One check in the eight-category validation taxonomy (computational, data,
semantic, statistical, sensitivity, alternative-method, consistency, reproducibility). Each is a
pluggable unit implementing the `Validator` protocol in `analysis/validation/base.py`, selectively
invoked, not run unconditionally.

**Hypothesis** — A named, typed (primary/null/alternative/exploratory/competing) belief the agent
is testing, carrying evidence for and against and a status. `analysis/hypotheses.py`.

**Critic finding** — A structured, categorised objection produced by the adversarial critic (e.g.
confounding, selection bias, Simpson's paradox). Never edits the underlying result; the main agent
decides how to react. `analysis/critic.py`.

**Competing analysis / route** — One of several valid analytical approaches to the same question,
run and compared explicitly rather than the agent silently picking one. `analysis/competing.py`.

**Confidence** — An explainable, component-derived assessment of how much the conclusion can be
trusted (data completeness, sample size, method robustness, verification status, method
agreement, sensitivity, unresolved assumptions, model uncertainty). Never a bare LLM-generated
percentage. `analysis/confidence.py`.

**Stop verdict** — One of `answerable`, `answerable_with_caveats`, `insufficient_evidence`,
`cannot_answer`. The latter two are success states, not failures. `analysis/stopping.py`.

**Analysis run** — An immutable snapshot of one completed turn: inputs, plan and its revisions,
executions, outputs, evidence, validations, warnings, environment versions. Reports render from a
run id, never from live mutable state. `analysis/runs.py`.

**Tier** — Existing concept (`TierBudget`, `compact`/`balanced`/`full`) that gates how much of the
above runs on a given turn. New analytical stages are tier-gated the same way existing ones
(reflection, verification, subagents) already are.
