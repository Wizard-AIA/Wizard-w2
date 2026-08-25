"""Adaptive data understanding -- PLAN.md Layer 2.

Built on `CatalogEngine`'s per-column profile and `StatisticalToolkit`'s correlation primitive;
this module adds table-level structure those don't: grain, join-key consistency across tables,
temporal coverage and target leakage. Each detector is a single pass over already-profiled
columns, so `understand()` costs no LLM round trip and is safe to run before the first prompt of
a turn -- see `orchestrator._ensure_understanding`, its only caller.

Adaptive, not wholesale: join-key checks only run with more than one table loaded; leakage and
temporal checks only run when a target or time column is actually given, since guessing one from
column names would be exactly the kind of fabricated certainty PLAN.md rules out.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.core.tools.catalog import CatalogEngine
from src.core.tools.stats import StatisticalToolkit


#: Column-name fragments that make a column a grain candidate when no column is fully unique.
_IDENTIFIER_HINTS = ("id", "uuid", "guid", "key", "code")

#: Analytical types for which a correlation-with-target scan is worth its cost.
LEAKAGE_TYPES = frozenset({"predictive", "model_based", "hypothesis_test", "inferential"})

#: Analytical types for which temporal coverage is worth reporting.
TEMPORAL_TYPES = frozenset({"forecasting", "longitudinal", "cohort"})


def candidate_identifiers(df: pd.DataFrame) -> list[str]:
    """Columns whose values are unique and non-null across every row -- primary-key candidates."""
    rows = len(df)
    if rows == 0:
        return []
    candidates = []
    for column in df.columns:
        try:
            if df[column].notna().all() and df[column].nunique(dropna=False) == rows:
                candidates.append(str(column))
        except TypeError:
            continue
    return candidates


def infer_grain(df: pd.DataFrame) -> dict[str, Any]:
    """What one row represents, and whether the table actually delivers it.

    A frame with no fully-unique column but a name-hinted identifier that repeats is "mixed
    grain" -- the table's own naming claims a per-id record but rows for the same id recur, which
    silently changes what a `groupby` or a `sum` over it means.
    """
    identifiers = candidate_identifiers(df)
    if identifiers:
        return {"grain": "single_column", "columns": identifiers, "mixed": False}

    hinted = [str(c) for c in df.columns if any(hint in str(c).lower() for hint in _IDENTIFIER_HINTS)]
    duplicated = {column: int(df[column].duplicated().sum()) for column in hinted if df[column].duplicated().any()}
    if duplicated:
        return {"grain": "mixed", "columns": list(duplicated), "duplicate_counts": duplicated, "mixed": True}

    unique_rows = int(df.drop_duplicates().shape[0])
    return {"grain": "row" if unique_rows == len(df) else "mixed", "columns": [], "mixed": unique_rows != len(df)}


def cardinality_profile(df: pd.DataFrame, catalog: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Per-column uniqueness, reusing the catalog's already-computed unique counts."""
    catalog = catalog or CatalogEngine.analyze(df)
    rows = catalog.get("global_quality", {}).get("rows") or len(df) or 1
    profile: dict[str, dict[str, Any]] = {}
    for column, info in catalog.get("columns", {}).items():
        unique = info.get("quality", {}).get("unique_values", -1)
        ratio = round(unique / rows, 4) if unique >= 0 else None
        profile[column] = {
            "unique_values": unique,
            "unique_ratio": ratio,
            "high_cardinality": bool(ratio is not None and ratio > 0.9 and rows > 20),
        }
    return profile


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def type_anomalies(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Object columns whose values are inconsistently typed -- some numeric-looking, some not."""
    anomalies: list[dict[str, Any]] = []
    for column in df.columns:
        if df[column].dtype != object:
            continue
        values = df[column].dropna().astype(str)
        if values.empty:
            continue
        ratio = float(values.map(_looks_numeric).mean())
        if 0 < ratio < 1:
            anomalies.append(
                {
                    "column": str(column),
                    "numeric_like_ratio": round(ratio, 4),
                    "reason": "some values look numeric and some do not",
                }
            )
    return anomalies


def candidate_join_keys(tables: dict[str, pd.DataFrame]) -> list[dict[str, str]]:
    """Column pairs across two loaded tables plausible as a join key -- same name on both sides."""
    names = list(tables)
    pairs: list[dict[str, str]] = []
    for i, left_name in enumerate(names):
        for right_name in names[i + 1 :]:
            shared = set(map(str, tables[left_name].columns)) & set(map(str, tables[right_name].columns))
            pairs.extend(
                {"left_table": left_name, "right_table": right_name, "left_column": column, "right_column": column}
                for column in shared
            )
    return pairs


def _normalised_keys(series: pd.Series) -> set[str]:
    """Values as bare digit strings -- "01", "1" and 1 all become "1" -- for spotting a join key
    that only *looks* mismatched because of formatting rather than because it refers to
    different entities.
    """
    stripped = series.dropna().astype(str).str.strip().str.lstrip("0")
    return {value or "0" for value in stripped}


def referential_consistency(left: pd.DataFrame, left_key: str, right: pd.DataFrame, right_key: str) -> dict[str, Any]:
    """Whether two tables' key columns actually line up.

    Catches a dirty join key before a generated merge silently drops rows or explodes them: a raw
    match rate near zero that jumps once values are normalised means the same entities are
    present on both sides, just formatted differently (int vs. zero-padded string, for example).
    """
    left_values = left[left_key].dropna()
    right_values_set = set(right[right_key].dropna())
    raw_overlap = sum(1 for value in left_values if value in right_values_set)
    raw_match_rate = raw_overlap / len(left_values) if len(left_values) else 0.0

    normalised_overlap = len(_normalised_keys(left_values) & _normalised_keys(right[right_key]))
    normalised_match_rate = normalised_overlap / len(left_values) if len(left_values) else 0.0

    dtype_mismatch = str(left[left_key].dtype) != str(right[right_key].dtype)
    dirty = normalised_match_rate > raw_match_rate + 0.2

    return {
        "left_key": left_key,
        "right_key": right_key,
        "raw_match_rate": round(raw_match_rate, 4),
        "normalised_match_rate": round(normalised_match_rate, 4),
        "dtype_mismatch": dtype_mismatch,
        "dirty": dirty,
        "orphaned_left": int(len(left_values) - raw_overlap),
    }


def check_join_keys(tables: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    """Referential consistency for every plausible join key shared across the loaded tables."""
    findings = []
    for pair in candidate_join_keys(tables):
        left, right = tables[pair["left_table"]], tables[pair["right_table"]]
        findings.append({**pair, **referential_consistency(left, pair["left_column"], right, pair["right_column"])})
    return findings


def temporal_coverage(df: pd.DataFrame, column: str) -> dict[str, Any]:
    """Date range and density of a time column -- gaps here silently bias a trend or forecast."""
    series = pd.to_datetime(df[column], errors="coerce", format="mixed").dropna()
    if series.empty:
        return {"column": column, "rows": 0}
    return {
        "column": column,
        "rows": int(len(series)),
        "start": series.min().isoformat(),
        "end": series.max().isoformat(),
        "span_days": int((series.max() - series.min()).days),
        "distinct_days": int(series.dt.floor("D").nunique()),
    }


def leakage_indicators(df: pd.DataFrame, target: str, threshold: float = 0.98) -> list[dict[str, Any]]:
    """Features almost perfectly correlated with the target -- often the target itself, restated
    or lightly transformed, rather than a real predictor.
    """
    if target not in df.columns:
        return []
    return [
        {
            "feature": entry["feature"],
            "correlation": entry["correlation"],
            "reason": "near-perfect correlation with the target",
        }
        for entry in StatisticalToolkit.correlation_analysis(df, target)
        if abs(entry["correlation"]) >= threshold
    ]


def understand(
    df: pd.DataFrame,
    *,
    tables: dict[str, pd.DataFrame] | None = None,
    target: str | None = None,
    time_column: str | None = None,
    analytical_type: str | None = None,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The single entry point: always-cheap structural facts, plus objective-driven checks.

    Grain, cardinality and type anomalies run every time -- a single pass over columns already
    profiled by `CatalogEngine`. Join-key checks run whenever more than one table is loaded, since
    that is a structural fact independent of what was asked. Leakage and temporal checks run only
    when a target or time column is actually supplied, and only when `analytical_type` (when
    given) says they are relevant -- never guessed from column names.
    """
    catalog = catalog or CatalogEngine.analyze(df)
    result: dict[str, Any] = {
        "grain": infer_grain(df),
        "cardinality": cardinality_profile(df, catalog),
        "type_anomalies": type_anomalies(df),
        "join_keys": [],
        "leakage": [],
        "temporal": None,
    }

    if tables and len(tables) > 1:
        result["join_keys"] = check_join_keys(tables)

    if target and target in df.columns and (analytical_type is None or analytical_type in LEAKAGE_TYPES):
        result["leakage"] = leakage_indicators(df, target)

    if time_column and time_column in df.columns and (analytical_type is None or analytical_type in TEMPORAL_TYPES):
        result["temporal"] = temporal_coverage(df, time_column)

    return result


def render(profile: dict[str, Any]) -> str:
    """Human-readable lines for only the findings worth a person's attention.

    Mirrors `prompts._quality_warnings`: silence when nothing is wrong, rather than restating
    "everything is fine" on top of the schema table already shown.
    """
    lines: list[str] = []

    grain = profile.get("grain") or {}
    if grain.get("mixed"):
        columns = ", ".join(grain.get("columns") or [])
        subject = f"`{columns}`" if columns else "no column"
        lines.append(f"- Mixed grain: {subject} uniquely identifies a row; duplicate keys exist.")

    for entry in profile.get("join_keys") or []:
        if entry.get("dirty"):
            reason = "dtype mismatch" if entry.get("dtype_mismatch") else "formatting mismatch"
            lines.append(
                f"- Dirty join key: `{entry['left_table']}.{entry['left_column']}` vs "
                f"`{entry['right_table']}.{entry['right_column']}` -- raw match rate "
                f"{entry['raw_match_rate']:.0%}, {entry['normalised_match_rate']:.0%} once normalised ({reason})."
            )

    for entry in profile.get("leakage") or []:
        lines.append(
            f"- Possible target leakage: `{entry['feature']}` correlates {entry['correlation']} with the target."
        )

    for entry in profile.get("type_anomalies") or []:
        lines.append(
            f"- `{entry['column']}` mixes numeric-looking and non-numeric values "
            f"({entry['numeric_like_ratio']:.0%} numeric-like)."
        )

    if not lines:
        return ""
    return "\n".join(["Data understanding:", *lines])
