"""Shared contracts for the pluggable validation framework -- PLAN.md Layer 4.

`Finding` is what a single validator has to say; `ValidationContext` is the read-only evidence
every validator sees, gathered once per turn so no validator reaches back into the orchestrator,
the LLM, or the executor -- the one exception, the computational validator, is fed its recomputed
status and detail rather than re-deriving them, so it needs no special access either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd


Severity = Literal["info", "warning", "error"]


@dataclass
class Finding:
    """One validator's verdict on one aspect of the turn's result."""

    validator: str
    severity: Severity
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"validator": self.validator, "severity": self.severity, "message": self.message, "detail": self.detail}


@dataclass
class ValidationContext:
    """Everything a validator might need about one turn, gathered once."""

    instruction: str = ""
    plan: str = ""
    code: str = ""
    output: str = ""
    df: pd.DataFrame | None = None
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: Phase 4's data-understanding profile, if it was computed this turn.
    understanding: dict[str, Any] | None = None
    #: The `analysis.methods` registry key the model named, if any -- unset until an objective
    #: names a method explicitly, which no phase yet does end-to-end.
    method: str | None = None
    #: Set by the orchestrator from `_verify`'s own recomputation, so the computational validator
    #: costs no second LLM call or execution of its own.
    recomputation_status: str | None = None
    recomputation_detail: str = ""


@runtime_checkable
class Validator(Protocol):
    """A single lens for judging whether a turn's result should be trusted."""

    name: str

    def applicable(self, ctx: ValidationContext) -> bool:
        """Whether this validator has anything to say about this turn at all."""
        ...

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        """Findings for this turn; empty when applicable but clean."""
        ...
