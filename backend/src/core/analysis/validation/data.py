"""Data-quality validation: re-surfaces Phase 4's understanding profile as findings.

No new detection here -- `understanding.understand()` already found grain, join-key and leakage
problems for the prompt; this validator is the same evidence read as a trust judgement on the
result rather than a heads-up before the model started.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


class DataValidator:
    name = "data"

    def applicable(self, ctx: ValidationContext) -> bool:
        return bool(ctx.understanding)

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        profile = ctx.understanding or {}
        findings: list[Finding] = []

        grain = profile.get("grain") or {}
        if grain.get("mixed"):
            columns = ", ".join(grain.get("columns") or []) or "an unnamed column"
            findings.append(Finding(self.name, "warning", f"The data's grain is mixed on {columns}.", str(grain)))

        for entry in profile.get("join_keys") or []:
            if entry.get("dirty"):
                message = (
                    f"Dirty join key: `{entry['left_table']}.{entry['left_column']}` vs "
                    f"`{entry['right_table']}.{entry['right_column']}`."
                )
                findings.append(Finding(self.name, "warning", message, str(entry)))

        for entry in profile.get("leakage") or []:
            findings.append(
                Finding(self.name, "error", f"Possible target leakage via '{entry['feature']}'.", str(entry))
            )

        for entry in profile.get("type_anomalies") or []:
            findings.append(
                Finding(self.name, "warning", f"'{entry['column']}' mixes numeric and non-numeric values.", str(entry))
            )

        return findings
