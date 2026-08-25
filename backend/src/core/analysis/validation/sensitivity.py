"""Sensitivity validation: flags an aggregate computed over a column outliers could be swinging.

Reuses `StatisticalToolkit.detect_outliers` rather than a second outlier pass -- the same IQR
check the catalog already runs per column, applied here only to columns the generated code
actually touched.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext
from src.core.tools.stats import StatisticalToolkit


_AGGREGATE_MARKERS = (".mean(", ".sum(", ".median(", ".std(")


def _is_mentioned(code: str, column: str) -> bool:
    return f"'{column}'" in code or f'"{column}"' in code or f".{column}" in code


class SensitivityValidator:
    name = "sensitivity"

    def applicable(self, ctx: ValidationContext) -> bool:
        return ctx.df is not None and any(marker in ctx.code for marker in _AGGREGATE_MARKERS)

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        findings: list[Finding] = []
        for column in ctx.df.select_dtypes(include="number").columns:
            if not _is_mentioned(ctx.code, str(column)):
                continue
            info = StatisticalToolkit.detect_outliers(ctx.df, str(column))
            if info["outlier_count"] > 0 and info["outlier_percentage"] >= 5:
                findings.append(
                    Finding(
                        self.name,
                        "warning",
                        f"'{column}' has {info['outlier_percentage']}% outliers; an aggregate over it may be "
                        "sensitive to them.",
                        str(info),
                    )
                )
        return findings
