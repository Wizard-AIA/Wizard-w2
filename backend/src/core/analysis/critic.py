"""Adversarial critic -- PLAN.md Layer 5 / the Section 10 catalogue of common analytical flaws.

Deterministic detectors run first, the same "check first, escalate only if something is already
suspicious" shape `council.py` already uses -- no detector here calls an LLM, because every check
below has a clean yes/no answer from the evidence already computed this turn. Categories the
catalogue names but that have no reliable deterministic signal (confounding in general, selection
bias, survivorship, temporal leakage, denominator errors) are left undetected rather than guessed
at, the same honest boundary `understanding.py` draws around leakage and temporal coverage.

Rule 4: the critic never edits a result. A finding only carries a *suggested* reaction; applying
it is the model's job when it writes the final answer (see `orchestrator._answer` and
`prompts.create_answer_prompt`'s `critic_findings` block), never this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from src.core.analysis.competing import applicability_kwargs, detect_method
from src.core.analysis.methods import run_method
from src.core.analysis.validation.base import ValidationContext
from src.core.analysis.validation.semantic import check_chart_legibility
from src.core.analysis.validation.statistical import check_significance_claims


Category = Literal[
    "simpsons_paradox",
    "leakage",
    "join",
    "aggregation_artifact",
    "misleading_visualisation",
    "wrong_test",
    "multiple_comparisons",
    "overfitting",
    "unsupported_causal_language",
    "overclaiming",
]
Reaction = Literal["accept", "revise", "re_analyse", "weaken", "mark_unresolved"]


@dataclass
class CriticFinding:
    """One flaw the critic caught, and what it thinks the agent should do about it."""

    category: Category
    severity: Literal["info", "warning", "error"]
    message: str
    detail: str = ""
    suggested_reaction: Reaction = "mark_unresolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
            "suggested_reaction": self.suggested_reaction,
        }


# --------------------------------------------------------------------------- #
# Simpson's paradox
# --------------------------------------------------------------------------- #
def candidate_grouping_columns(df: pd.DataFrame, max_categories: int = 8) -> list[str]:
    """Columns structurally plausible as a group or confounder: few enough distinct values to
    segment by, the same structural search `understanding.candidate_join_keys` already does for
    join keys rather than guessing a column's meaning from its name."""
    return [str(column) for column in df.columns if 2 <= df[column].nunique(dropna=True) <= max_categories]


def detect_simpsons_paradox(df: pd.DataFrame, outcome: str, group: str, segment: str) -> CriticFinding | None:
    """A group ordering that reverses in every segment once a confounder is controlled for."""
    if outcome not in df.columns or group not in df.columns or segment not in df.columns:
        return None
    overall = df.groupby(group)[outcome].mean()
    if overall.shape[0] != 2 or overall.isna().any():
        return None
    overall_winner = overall.idxmax()

    per_segment = df.groupby([segment, group])[outcome].mean().unstack(group).dropna()
    if per_segment.shape[1] != 2 or per_segment.empty:
        return None
    segment_winners = per_segment.idxmax(axis=1)
    if (segment_winners == overall_winner).any():
        return None  # at least one segment agrees with the aggregate -- not a reversal in every segment

    return CriticFinding(
        category="simpsons_paradox",
        severity="error",
        message=(
            f"'{group}' favours {overall_winner!r} overall on '{outcome}', but every '{segment}' segment "
            "favours the other group -- the aggregate reverses once segmented."
        ),
        detail=f"overall={overall.round(4).to_dict()}",
        suggested_reaction="revise",
    )


def find_simpsons_paradoxes(df: pd.DataFrame, outcome: str) -> list[CriticFinding]:
    """Searches candidate group/confounder column pairs for a Simpson's-paradox reversal."""
    candidates = candidate_grouping_columns(df)
    findings = []
    for group in candidates:
        for segment in candidates:
            if segment == group:
                continue
            finding = detect_simpsons_paradox(df, outcome, group, segment)
            if finding:
                findings.append(finding)
    return findings


def _aggregated_numeric_column(df: pd.DataFrame, code: str) -> str | None:
    """The first numeric column the code actually references -- never a guess at intent."""
    for column in df.select_dtypes(include="number").columns:
        if f"'{column}'" in code or f'"{column}"' in code or f".{column}" in code:
            return str(column)
    return None


# --------------------------------------------------------------------------- #
# Method misuse
# --------------------------------------------------------------------------- #
def detect_wrong_test(method: str, df: pd.DataFrame, **kwargs: Any) -> CriticFinding | None:
    """Runs a named method through `analysis.methods`'s applicability check, unchanged -- a
    refusal there is a wrong-test finding here."""
    result = run_method(method, df, **kwargs)
    if result["status"] != "refused":
        return None
    message = f"'{method}' does not fit this data: {'; '.join(result['reasons'])}."
    detail = f"alternative: {result['alternative']}" if result["alternative"] else ""
    return CriticFinding(
        category="wrong_test", severity="error", message=message, detail=detail, suggested_reaction="revise"
    )


def detect_wrong_test_from_context(ctx: ValidationContext) -> CriticFinding | None:
    """Wires `detect_wrong_test` into the turn's own evidence: the method named in `ctx.method`
    (or, failing that, the first registered method literally present in the executed code), and
    the columns that code actually references -- never a method or column pairing invented for
    the check, the same restraint `competing._more_appropriate` applies to route comparison."""
    if ctx.df is None:
        return None
    method = ctx.method or detect_method(ctx.code)
    if method is None:
        return None
    kwargs = applicability_kwargs(method, ctx.code, ctx.df)
    if kwargs is None:
        return None
    return detect_wrong_test(method, ctx.df, **kwargs)


# --------------------------------------------------------------------------- #
# Multiple comparisons / overfitting
# --------------------------------------------------------------------------- #
_TEST_MARKERS = (
    "ttest",
    "pearsonr",
    "spearmanr",
    "chi2_contingency",
    "f_oneway",
    "kruskal",
    "mannwhitneyu",
    "wilcoxon",
)
_CORRECTION_MARKERS = ("correct_p_values", "bonferroni", "fdr_bh", "multipletests")


def detect_multiple_comparisons(code: str) -> list[CriticFinding]:
    test_count = sum(code.count(marker) for marker in _TEST_MARKERS)
    if test_count < 2 or any(marker in code for marker in _CORRECTION_MARKERS):
        return []
    message = f"{test_count} statistical tests were run with no correction for multiple comparisons."
    return [CriticFinding("multiple_comparisons", "warning", message, suggested_reaction="weaken")]


_FIT_MARKERS = (".fit(",)
_HOLDOUT_MARKERS = ("train_test_split", "cross_val_score", "cross_validate", "KFold")
_SCORE_MARKERS = (".score(", "accuracy_score", "r2_score", "mean_squared_error")


def detect_overfitting(code: str) -> list[CriticFinding]:
    if not any(marker in code for marker in _FIT_MARKERS):
        return []
    if any(marker in code for marker in _HOLDOUT_MARKERS):
        return []
    if not any(marker in code for marker in _SCORE_MARKERS):
        return []
    message = "A model was fit and scored with no held-out evaluation (no train/test split or cross-validation)."
    return [CriticFinding("overfitting", "warning", message, suggested_reaction="revise")]


# --------------------------------------------------------------------------- #
# Reused signals: leakage, dirty joins, mixed-grain aggregation, chart legibility, overclaiming
# --------------------------------------------------------------------------- #
def detect_leakage(ctx: ValidationContext) -> list[CriticFinding]:
    return [
        CriticFinding(
            "leakage", "error", f"Possible target leakage via '{entry['feature']}'.", str(entry), "re_analyse"
        )
        for entry in (ctx.understanding or {}).get("leakage") or []
    ]


def detect_join_problems(ctx: ValidationContext) -> list[CriticFinding]:
    findings = []
    for entry in (ctx.understanding or {}).get("join_keys") or []:
        if entry.get("dirty"):
            message = (
                f"Dirty join key: `{entry['left_table']}.{entry['left_column']}` vs "
                f"`{entry['right_table']}.{entry['right_column']}`."
            )
            findings.append(CriticFinding("join", "warning", message, str(entry), "revise"))
    return findings


def detect_aggregation_artifact(ctx: ValidationContext) -> list[CriticFinding]:
    grain = (ctx.understanding or {}).get("grain") or {}
    if not grain.get("mixed") or "groupby" in ctx.code or "drop_duplicates" in ctx.code:
        return []
    columns = ", ".join(grain.get("columns") or []) or "an unnamed column"
    message = f"The data's grain is mixed on {columns}; aggregating without grouping or deduplicating may double-count."
    return [CriticFinding("aggregation_artifact", "warning", message, str(grain), "revise")]


def detect_misleading_visualisation(ctx: ValidationContext) -> list[CriticFinding]:
    return [
        CriticFinding("misleading_visualisation", finding.severity, finding.message, finding.detail, "revise")
        for finding in check_chart_legibility(ctx.code)
    ]


def detect_overclaiming(ctx: ValidationContext) -> list[CriticFinding]:
    findings = []
    for finding in check_significance_claims(ctx.plan, ctx.code, ctx.output):
        category: Category = "unsupported_causal_language" if "causal" in finding.message.lower() else "overclaiming"
        findings.append(CriticFinding(category, finding.severity, finding.message, finding.detail, "weaken"))
    return findings


# --------------------------------------------------------------------------- #
# Aggregate entry point
# --------------------------------------------------------------------------- #
def critique(ctx: ValidationContext) -> list[CriticFinding]:
    """Runs every detector applicable to this turn's evidence."""
    findings: list[CriticFinding] = [
        *detect_leakage(ctx),
        *detect_join_problems(ctx),
        *detect_aggregation_artifact(ctx),
        *detect_misleading_visualisation(ctx),
        *detect_overclaiming(ctx),
        *detect_multiple_comparisons(ctx.code),
        *detect_overfitting(ctx.code),
    ]
    wrong_test = detect_wrong_test_from_context(ctx)
    if wrong_test:
        findings.append(wrong_test)
    if ctx.df is not None:
        outcome = _aggregated_numeric_column(ctx.df, ctx.code)
        if outcome:
            findings.extend(find_simpsons_paradoxes(ctx.df, outcome))
    return findings
