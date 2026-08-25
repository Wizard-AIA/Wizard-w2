"""Technical report: the full audit trail -- dataset manifest, executed code, raw findings, and
every claim's provenance chain, for someone reproducing or auditing the turn.
"""

from __future__ import annotations

import time

from src.core.analysis.reports._shared import claim_trace_line, confidence_lines, warnings_lines
from src.core.analysis.reports.model import ReportModel


def _manifest_lines(model: ReportModel) -> list[str]:
    if not model.dataset_manifest:
        return []
    lines = ["## Dataset Manifest"]
    for entry in model.dataset_manifest:
        lines.append(
            f"- `{entry['name']}` ({entry['table_key']}): {entry['rows']} rows, "
            f"{len(entry['columns'])} columns, content hash `{entry['content_hash'][:12]}`"
        )
    return lines


def _step_lines(model: ReportModel) -> list[str]:
    if not model.steps:
        return []
    lines = ["## Executed Steps"]
    for index, step in enumerate(model.steps, start=1):
        status = "ok" if step.get("ok", True) else "failed"
        lines.append(f"### {index}. {step.get('goal')} ({status})")
        lines.append(f"```python\n{step.get('code', '')}\n```")
    return lines


def _claim_lines(model: ReportModel) -> list[str]:
    if not model.claims:
        return []
    return ["## Claims and Provenance", *[claim_trace_line(claim) for claim in model.claims]]


def _validation_lines(model: ReportModel) -> list[str]:
    if not model.validations:
        return []
    lines = ["## Validations"]
    for finding in model.validations:
        detail = f" ({finding['detail']})" if finding.get("detail") else ""
        lines.append(f"- **{finding.get('validator')}** [{finding.get('severity')}]: {finding.get('message')}{detail}")
    return lines


def _critic_lines(model: ReportModel) -> list[str]:
    if not model.critic_findings:
        return []
    lines = ["## Critic Findings"]
    for finding in model.critic_findings:
        detail = f" ({finding['detail']})" if finding.get("detail") else ""
        lines.append(
            f"- **{finding.get('category')}** [{finding.get('severity')}]: {finding.get('message')}{detail} "
            f"-- suggested: {finding.get('suggested_reaction')}"
        )
    return lines


def _confidence_component_lines(model: ReportModel) -> list[str]:
    components = (model.confidence or {}).get("components") or []
    if not components:
        return []
    lines = ["## Confidence Components"]
    lines.extend(f"- {c.get('name')}: {c.get('level')} -- {c.get('reason')}" for c in components)
    return lines


def render(model: ReportModel) -> str:
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(model.created_at)) if model.created_at else "unknown"
    lines = [
        "# Technical Report",
        "",
        f"- Session: `{model.session_id}`, message `{model.message_id}`, captured {created}",
        f"- Tool versions: {', '.join(f'{k} {v}' for k, v in model.tool_versions.items()) or 'unrecorded'}",
        "",
        *_manifest_lines(model),
        "",
        *_step_lines(model),
        "",
        *_claim_lines(model),
        "",
        *_validation_lines(model),
        "",
        *_critic_lines(model),
        "",
        "## Confidence",
        *confidence_lines(model),
        *_confidence_component_lines(model),
        "",
        *warnings_lines(model),
    ]
    return "\n".join(lines).strip() + "\n"
