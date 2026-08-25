"""The analytical plan as a structured, revisable object -- PLAN.md Layer 5 / ADR 0002.

`state.plan` (the string every existing prompt already embeds) stays exactly what it always
was: the current revision's raw text. This module adds structure and history alongside it, not
instead of it -- the defining property PLAN.md asks for is "observation -> plan update", not
"plan -> execution", so a revision is appended, never overwritten.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


#: A numbered ("1. ...") or bulleted ("- "/"* ") line -- the only shape the planning prompts
#: ask for (see prompts.create_planning_prompt), so this is a mechanical extraction, not a
#: semantic one. Prose that doesn't match this shape simply yields no steps, never a guess.
_STEP_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$")
_HYPOTHESIS_LINE = re.compile(
    r"^\s*(?:hypothesis|primary hypothesis|null hypothesis|alternative hypothesis)\s*:\s*(.+)$", re.I
)


def parse_intended_analyses(text: str) -> list[str]:
    """Pulls the numbered/bulleted steps out of a plan's prose, in order."""
    steps: list[str] = []
    for line in (text or "").splitlines():
        match = _STEP_LINE.match(line)
        if match:
            step = match.group(1).strip()
            if step:
                steps.append(step)
    return steps


def parse_hypotheses(text: str) -> list[str]:
    """Extract explicitly labelled hypotheses without inventing one from prose."""
    hypotheses: list[str] = []
    for line in (text or "").splitlines():
        match = _HYPOTHESIS_LINE.match(line)
        if match and match.group(1).strip():
            hypotheses.append(match.group(1).strip())
    return hypotheses


@dataclass
class PlanRevision:
    """One recorded state of the plan, never mutated once appended."""

    index: int
    text: str
    why: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "text": self.text, "why": self.why, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanRevision:
        return cls(
            index=int(data.get("index", 0)),
            text=str(data.get("text", "")),
            why=str(data.get("why", "")),
            at=float(data.get("at", 0.0)),
        )


@dataclass
class AnalyticalPlan:
    """A structured, revisable analytical plan -- PLAN.md Layer 5.

    Most fields are populated by later phases that own the mechanism for them (hypotheses by
    Phase 7, validation_criteria by Phase 6, stop_criteria by Phase 9, ...). This phase
    establishes the shape, the mechanical `intended_analyses` extraction, and revision history.
    """

    hypotheses: list[str] = field(default_factory=list)
    intended_analyses: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    validation_criteria: list[str] = field(default_factory=list)
    stop_criteria: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    open_uncertainties: list[str] = field(default_factory=list)
    revisions: list[PlanRevision] = field(default_factory=list)

    @property
    def current_text(self) -> str:
        """The most recent revision's raw text, or "" before any revision is recorded."""
        return self.revisions[-1].text if self.revisions else ""

    def revise(self, text: str, why: str = "") -> PlanRevision:
        """Appends a new revision and re-derives the mechanical step list. Never overwrites."""
        revision = PlanRevision(index=len(self.revisions), text=text, why=why)
        self.revisions.append(revision)
        self.intended_analyses = parse_intended_analyses(text)
        self.hypotheses = parse_hypotheses(text)
        return revision

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypotheses": self.hypotheses,
            "intended_analyses": self.intended_analyses,
            "required_evidence": self.required_evidence,
            "assumptions": self.assumptions,
            "dependencies": self.dependencies,
            "expected_outputs": self.expected_outputs,
            "validation_criteria": self.validation_criteria,
            "stop_criteria": self.stop_criteria,
            "fallbacks": self.fallbacks,
            "open_uncertainties": self.open_uncertainties,
            "revisions": [revision.to_dict() for revision in self.revisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalyticalPlan:
        return cls(
            hypotheses=list(data.get("hypotheses") or []),
            intended_analyses=list(data.get("intended_analyses") or []),
            required_evidence=list(data.get("required_evidence") or []),
            assumptions=list(data.get("assumptions") or []),
            dependencies=list(data.get("dependencies") or []),
            expected_outputs=list(data.get("expected_outputs") or []),
            validation_criteria=list(data.get("validation_criteria") or []),
            stop_criteria=list(data.get("stop_criteria") or []),
            fallbacks=list(data.get("fallbacks") or []),
            open_uncertainties=list(data.get("open_uncertainties") or []),
            revisions=[PlanRevision.from_dict(item) for item in (data.get("revisions") or [])],
        )
