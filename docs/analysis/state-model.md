# Analytical State Model

Defines the shape `analysis/state.py` (Phase 1) implements. This is a contract, not code — later
phases must not redesign these fields; they extend them.

## AnalyticalState

One per turn, persisted to the `analysis_state` table, attached to `RunState.analysis`.

| Field | Type | Populated by |
| :-- | :-- | :-- |
| `objective` | `AnalyticalObjective \| None` | `_orient`, from the plan prompt's structured extraction |
| `understanding` | `DataUnderstanding \| None` | Phase 4, from `_act_inspect` |
| `hypotheses` | `list[Hypothesis]` | Phase 7 |
| `assumptions` | delegates to `Investigation.assumptions` | existing `grounding.py` callers |
| `findings` | delegates to `Investigation.findings` | existing `investigation.note_finding` callers |
| `evidence_refs` | `list[str]` (evidence-graph node ids) | Phase 3 |
| `validations` | `list[ValidationResult]` | Phase 6 |
| `open_questions` | `list[str]` | Phase 2 (plan) and Phase 7 (critic) |
| `confidence` | `Confidence \| None` | Phase 9 |

Fields absent this turn stay `None`/empty rather than being fabricated — an unresolved objective
is represented, never guessed into false certainty (per PLAN.md's uncertainty rule).

## AnalyticalObjective

| Field | Type |
| :-- | :-- |
| `question` | `str` |
| `analytical_type` | `Literal["descriptive","diagnostic","inferential","predictive","exploratory","comparative","causal_looking","forecasting","anomaly","cohort","segmentation","longitudinal","multi_table","hypothesis_test","model_based","evidence_synthesis"] \| None` |
| `unit_of_analysis` | `str \| None` |
| `population` | `str \| None` |
| `time_dimension` | `str \| None` |
| `likely_variables` | `dict[str, list[str]]` (`"dependent"`/`"independent"` → column names) |
| `constraints` | `list[str]` |
| `expected_output` | `str \| None` |
| `ambiguity` | `list[str]` — explicit, never silently resolved |

## Compatibility rule

`Investigation.findings` / `.assumptions` remain the single source of truth for those two lists;
`AnalyticalState` exposes them as properties delegating to the `Investigation` instance already on
`RunState`, so no existing call site (`note_finding`, `note_assumption`, the `FINDING`/
`ASSUMPTION` events, `_finalize`'s persistence) changes behaviour or signature.

## Serialisation

`AnalyticalState.to_dict()` / `from_dict()` round-trip through JSON for the `analysis_state`
table and for the `FINAL` event payload. Dataclasses with `Literal`/`Enum` fields degrade to their
`.value` on dump and are validated (not guessed) on load — an unrecognised value is kept as `None`
plus a note in `ambiguity`/`open_questions`, never coerced to a plausible-looking default.
