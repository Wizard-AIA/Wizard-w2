"""Method registry -- PLAN.md Layer 3.

The model names an analysis method by key; this registry decides whether it actually runs. Each
entry pairs a `StatisticalToolkit` call with a deterministic applicability check, so a method that
does not fit the data is refused with a named reason and an alternative -- never run anyway and
rationalised afterwards. `StatisticalToolkit` itself already auto-picks the right *variant* of a
test (Welch vs. Student, ANOVA vs. Kruskal-Wallis); this registry instead catches the coarser
mistake of naming the wrong *family* of test for the data at hand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.tools.stats import StatisticalToolkit


def _numeric(df: pd.DataFrame, column: str) -> bool:
    return column in df.columns and pd.api.types.is_numeric_dtype(df[column].dtype)


def _check_two_group_numeric(df: pd.DataFrame, *, value_col: str, group_col: str) -> list[str]:
    reasons = [] if _numeric(df, value_col) else [f"'{value_col}' is not numeric"]
    groups = df[group_col].dropna().unique() if group_col in df.columns else []
    if len(groups) != 2:
        reasons.append(f"'{group_col}' has {len(groups)} groups; independent_t_test needs exactly 2")
    else:
        reasons += [
            f"group '{g}' has fewer than 2 observations"
            for g in groups
            if df.loc[df[group_col] == g, value_col].dropna().shape[0] < 2
        ]
    return reasons


def _check_multi_group_numeric(df: pd.DataFrame, *, value_col: str, group_col: str) -> list[str]:
    reasons = [] if _numeric(df, value_col) else [f"'{value_col}' is not numeric"]
    groups = df[group_col].dropna().unique() if group_col in df.columns else []
    if len(groups) < 3:
        reasons.append(f"'{group_col}' has {len(groups)} groups; one_way_anova needs at least 3")
    return reasons


def _check_paired_numeric(df: pd.DataFrame, *, col_a: str, col_b: str) -> list[str]:
    reasons = [f"'{c}' is not numeric" for c in (col_a, col_b) if not _numeric(df, c)]
    if not reasons and df[[col_a, col_b]].dropna().shape[0] < 3:
        reasons.append("fewer than 3 complete pairs")
    return reasons


def _check_categorical_pair(df: pd.DataFrame, *, col_a: str, col_b: str) -> list[str]:
    table = pd.crosstab(df[col_a], df[col_b])
    if table.shape[0] < 2 or table.shape[1] < 2:
        return ["chi_square_test needs at least two categories in each column"]
    return []


def _check_pearson(df: pd.DataFrame, *, col_a: str, col_b: str) -> list[str]:
    reasons = [f"'{c}' is not numeric" for c in (col_a, col_b) if not _numeric(df, c)]
    if reasons:
        return reasons
    paired = df[[col_a, col_b]].dropna()
    if len(paired) < 4:
        return ["fewer than 4 paired observations"]
    return [
        f"'{c}' is not normally distributed"
        for c in (col_a, col_b)
        if not StatisticalToolkit.check_normality(paired, c)["is_normal"]
    ]


def _check_spearman(df: pd.DataFrame, *, col_a: str, col_b: str) -> list[str]:
    reasons = [f"'{c}' is not numeric" for c in (col_a, col_b) if not _numeric(df, c)]
    if not reasons and df[[col_a, col_b]].dropna().shape[0] < 4:
        reasons.append("fewer than 4 paired observations")
    return reasons


def _check_linear_regression(df: pd.DataFrame, *, target: str, features: list[str]) -> list[str]:
    reasons = [f"target '{target}' is not numeric"] if not _numeric(df, target) else []
    reasons += [f"feature '{f}' is not numeric" for f in features if not _numeric(df, f)]
    rows = df[[target, *features]].dropna().shape[0]
    if rows <= len(features) + 1:
        reasons.append(f"only {rows} complete rows for {len(features)} predictors")
    return reasons


def _check_logistic_regression(df: pd.DataFrame, *, target: str, features: list[str]) -> list[str]:
    classes = df[target].dropna().unique() if target in df.columns else []
    reasons = [] if len(classes) == 2 else [f"target '{target}' has {len(classes)} classes; needs exactly 2"]
    reasons += [f"feature '{f}' is not numeric" for f in features if not _numeric(df, f)]
    rows = df[[target, *features]].dropna().shape[0]
    if rows <= len(features) + 1:
        reasons.append(f"only {rows} complete rows for {len(features)} predictors")
    return reasons


@dataclass(frozen=True)
class MethodSpec:
    """One selectable method: what it needs to be valid, and what to run once it is."""

    name: str
    description: str
    assumptions: tuple[str, ...]
    check: Callable[..., list[str]]
    run: Callable[..., dict[str, Any]]
    alternative: str | None


REGISTRY: dict[str, MethodSpec] = {
    "independent_t_test": MethodSpec(
        name="independent_t_test",
        description="Compares means of a numeric column between exactly two independent groups.",
        assumptions=("value column is numeric", "exactly two groups", "2+ observations per group"),
        check=_check_two_group_numeric,
        run=lambda df, *, value_col, group_col: StatisticalToolkit.compare_two_groups(df, value_col, group_col),
        alternative="one_way_anova",
    ),
    "one_way_anova": MethodSpec(
        name="one_way_anova",
        description="Compares means of a numeric column across three or more independent groups.",
        assumptions=("value column is numeric", "at least three groups"),
        check=_check_multi_group_numeric,
        run=lambda df, *, value_col, group_col: StatisticalToolkit.one_way_anova(df, value_col, group_col),
        alternative=None,
    ),
    "paired_comparison": MethodSpec(
        name="paired_comparison",
        description="Compares two numeric columns measured on the same rows.",
        assumptions=("both columns numeric", "3+ complete pairs"),
        check=_check_paired_numeric,
        run=lambda df, *, col_a, col_b: StatisticalToolkit.paired_comparison(df, col_a, col_b),
        alternative=None,
    ),
    "chi_square_test": MethodSpec(
        name="chi_square_test",
        description="Tests independence between two categorical columns.",
        assumptions=("both columns have at least two categories",),
        check=_check_categorical_pair,
        run=lambda df, *, col_a, col_b: StatisticalToolkit.chi_square_test(df, col_a, col_b),
        alternative=None,
    ),
    "pearson_correlation": MethodSpec(
        name="pearson_correlation",
        description="Linear correlation between two numeric columns.",
        assumptions=("both columns numeric", "4+ paired observations", "both columns approximately normal"),
        check=_check_pearson,
        run=lambda df, *, col_a, col_b: StatisticalToolkit.correlation_with_ci(df, col_a, col_b, method="pearson"),
        alternative="spearman_correlation",
    ),
    "spearman_correlation": MethodSpec(
        name="spearman_correlation",
        description="Rank correlation between two numeric columns; no normality assumption.",
        assumptions=("both columns numeric", "4+ paired observations"),
        check=_check_spearman,
        run=lambda df, *, col_a, col_b: StatisticalToolkit.correlation_with_ci(df, col_a, col_b, method="spearman"),
        alternative=None,
    ),
    "linear_regression": MethodSpec(
        name="linear_regression",
        description="OLS regression of a numeric target on one or more numeric features.",
        assumptions=("target and features numeric", "rows exceed predictors plus one"),
        check=_check_linear_regression,
        run=lambda df, *, target, features: StatisticalToolkit.linear_regression(df, target, features),
        alternative=None,
    ),
    "logistic_regression": MethodSpec(
        name="logistic_regression",
        description="Binary logistic regression of a two-class target on numeric features.",
        assumptions=("target has exactly two classes", "features numeric", "rows exceed predictors plus one"),
        check=_check_logistic_regression,
        run=lambda df, *, target, features: StatisticalToolkit.logistic_regression(df, target, features),
        alternative=None,
    ),
}


def run_method(name: str, df: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    """Runs a named method if -- and only if -- its assumptions hold for this data.

    A refusal names every violated assumption and, where one exists, the alternative method that
    would apply instead -- so an inappropriate test never silently runs (PLAN.md Rules 2 and 3).
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return {"status": "unknown_method", "method": name, "known_methods": sorted(REGISTRY)}
    reasons = spec.check(df, **kwargs)
    if reasons:
        return {"status": "refused", "method": name, "reasons": reasons, "alternative": spec.alternative}
    return {"status": "ok", "method": name, "result": spec.run(df, **kwargs)}
