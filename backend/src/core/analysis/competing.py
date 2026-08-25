"""Competing multi-method analysis -- PLAN.md Layer 6 / Phase 8.

Two or more analytical routes can answer the same question a different way -- a different named
method, a different assumption. Routed through the orchestrator's existing `_act_parallel` /
`SubagentSession` fan-out (see `orchestrator._compare_routes`), not a second concurrency
mechanism -- this module only compares what came back: do the routes' numeric conclusions agree,
and if not, which is better supported, using Phase 5's own applicability checks rather than a
fresh, unexplained judgment call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from src.core.agent.grounding import extract_numbers
from src.core.analysis.methods import REGISTRY


Verdict = Literal["agree", "disagree", "inconclusive"]

#: Objective shapes where two routes disagreeing is worth the extra fan-out to explain, mirroring
#: the same "ambiguity is a first-class field" boundary `objective.py` already draws.
_HIGH_IMPACT_TYPES = frozenset({"inferential", "hypothesis_test", "causal_looking", "comparative", "model_based"})


def is_high_impact_or_ambiguous(objective: Any) -> bool:
    """Whether this turn's objective justifies comparing competing routes at all."""
    if objective is None:
        return False
    if getattr(objective, "ambiguity", None):
        return True
    return getattr(objective, "analytical_type", None) in _HIGH_IMPACT_TYPES


@dataclass
class RouteResult:
    """One completed (or failed) branch's contribution to a route comparison."""

    branch: str
    method: str | None
    observation: str
    ok: bool
    code: str = ""


@dataclass
class RouteComparison:
    """The deterministic verdict over a set of routes -- never a silent pick."""

    verdict: Verdict
    routes: list[str]
    agreement_detail: str
    more_appropriate: str | None
    why: str
    residual_uncertainty: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "routes": self.routes,
            "agreement_detail": self.agreement_detail,
            "more_appropriate": self.more_appropriate,
            "why": self.why,
            "residual_uncertainty": self.residual_uncertainty,
        }


def detect_method(text: str) -> str | None:
    """The first registered method name literally present in ``text`` -- recognising a named
    entity the model already chose, never guessing one from the surrounding prose."""
    for name in REGISTRY:
        if name in (text or ""):
            return name
    return None


def _numeric_conclusion(observation: str) -> float | None:
    """The last numeric literal in a branch's output -- the headline figure is usually last."""
    for raw in reversed(extract_numbers(observation)):
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            continue
    return None


def _agrees(a: float, b: float, relative_tolerance: float = 0.05) -> bool:
    magnitude = max(abs(a), abs(b))
    if magnitude == 0:
        return a == b
    return abs(a - b) / magnitude <= relative_tolerance


def _is_numeric(df: pd.DataFrame, column: str) -> bool:
    return column in df.columns and pd.api.types.is_numeric_dtype(df[column].dtype)


def _columns_mentioned(code: str, df: pd.DataFrame) -> list[str]:
    """Column names from ``df`` that a branch's own code literally references, in appearance
    order -- the same "read the code, don't guess intent" pattern `critic._aggregated_numeric_column`
    already uses, so a route's applicability is checked against the columns it actually used."""
    found = []
    for column in df.columns:
        name = str(column)
        if (f"'{name}'" in code or f'"{name}"' in code or f".{name}" in code) and name not in found:
            found.append(name)
    return found


#: The only two `methods.REGISTRY` pairs Phase 8 knows how to build applicability kwargs for --
#: both share a parameter shape between the two named methods, so the mapping is unambiguous.
_CORRELATION_PAIR = ("pearson_correlation", "spearman_correlation")
_GROUP_COMPARISON_PAIR = ("independent_t_test", "one_way_anova")


def _check_kwargs(method: str, columns: list[str], df: pd.DataFrame) -> dict[str, Any] | None:
    """The keyword arguments a registry method's `check` needs, built from columns the route's
    own code already named. Returns None when Phase 8 does not know how to build them for this
    method rather than guessing a column pairing that was never stated."""
    if method in _CORRELATION_PAIR:
        numeric = [c for c in columns if _is_numeric(df, c)]
        return {"col_a": numeric[0], "col_b": numeric[1]} if len(numeric) >= 2 else None
    if method in _GROUP_COMPARISON_PAIR:
        numeric = [c for c in columns if _is_numeric(df, c)]
        categorical = [c for c in columns if not _is_numeric(df, c)]
        return {"value_col": numeric[0], "group_col": categorical[0]} if numeric and categorical else None
    return None


def applicability_kwargs(method: str, code: str, df: pd.DataFrame) -> dict[str, Any] | None:
    """Public wrapper over `_check_kwargs`/`_columns_mentioned` for callers outside route
    comparison -- the critic's wrong-test detector needs the same "read the code, don't guess
    intent" column resolution for a single named method."""
    return _check_kwargs(method, _columns_mentioned(code, df), df)


def _more_appropriate(routes: list[RouteResult], df: pd.DataFrame | None) -> tuple[str | None, str]:
    """Which named route's method actually fits this data, reusing Phase 5's applicability
    checks -- the same "the model names it, deterministic code checks it" split `methods.py` uses."""
    named = [route for route in routes if route.method and route.method in REGISTRY]
    if len(named) < 2 or df is None:
        return None, "The routes did not both name a registered method, or no dataset was available to check."

    checked, fits = [], []
    for route in named:
        kwargs = _check_kwargs(route.method, _columns_mentioned(route.code, df), df)
        if kwargs is None:
            continue
        checked.append(route)
        if not REGISTRY[route.method].check(df, **kwargs):
            fits.append(route)

    if len(checked) < 2:
        return (
            None,
            "The routes' column choices could not be identified from their code, so applicability was not checked.",
        )
    if len(fits) == 1:
        other = next(route for route in checked if route is not fits[0])
        return fits[0].method, f"'{fits[0].method}' fits this data's assumptions; '{other.method}' does not."
    if not fits:
        return None, "Neither named method's assumptions fit this data cleanly."
    return None, "Both named methods' assumptions fit this data; the disagreement is not explained by test choice."


def compare_routes(routes: list[RouteResult], df: pd.DataFrame | None = None) -> RouteComparison:
    """Compares two or more completed routes deterministically -- no LLM call, no silent pick."""
    labels = [route.branch for route in routes]
    numeric = [
        (route, value) for route in routes if route.ok and (value := _numeric_conclusion(route.observation)) is not None
    ]

    if len(numeric) < 2:
        return RouteComparison(
            verdict="inconclusive",
            routes=labels,
            agreement_detail="Fewer than two routes produced a comparable numeric conclusion.",
            more_appropriate=None,
            why="Not enough completed routes to compare.",
            residual_uncertainty="Which route is more trustworthy is unresolved.",
        )

    reference_value = numeric[0][1]
    disagreeing = [(route, value) for route, value in numeric[1:] if not _agrees(reference_value, value)]
    detail = ", ".join(f"{route.branch}={value:g}" for route, value in numeric)

    if not disagreeing:
        return RouteComparison(
            verdict="agree",
            routes=labels,
            agreement_detail=f"All routes converge: {detail}.",
            more_appropriate=None,
            why="The routes reached the same conclusion within tolerance.",
            residual_uncertainty="None beyond what each route already reported.",
        )

    more_appropriate, why = _more_appropriate([route for route, _ in numeric], df)
    residual = (
        f"'{more_appropriate}' is better supported, but the disagreement itself should be disclosed."
        if more_appropriate
        else "No named method's assumptions clearly fit better; report both figures and the disagreement."
    )
    return RouteComparison(
        verdict="disagree",
        routes=labels,
        agreement_detail=f"Routes disagree: {detail}.",
        more_appropriate=more_appropriate,
        why=why,
        residual_uncertainty=residual,
    )
