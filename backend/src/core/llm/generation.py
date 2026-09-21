"""Per-request generation policy and its provenance.

The provider layer receives one normalized configuration.  Keeping policy here
means callers can answer why a call received a particular output budget without
reverse engineering provider-specific client kwargs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.config import settings


PURPOSES = frozenset({"plan", "decision", "code", "answer", "review", "converse"})
_MIN_OUTPUT_TOKENS = 64

# A direct or inspect answer states a result the code already computed. It does
# not get the allowance of an agentic answer that has to explain an analysis.
_DIRECT_ANSWER_CAP = 1536

# Deep mode is allowed longer plans, programs and answers. Nothing is scaled
# down: a program cut off mid-statement is a failed call, so fast mode saves
# calls (no planner, no verification), not tokens per call.
_DEEP_SCALE = 1.5
_DEEP_SCALED = frozenset({"plan", "code", "answer"})


@dataclass(frozen=True)
class GenerationConfig:
    """Provider-neutral generation settings.

    ``None`` means the caller did not request that optional parameter.  An
    adapter must omit it when the target provider does not support it.
    """

    max_output_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop: tuple[str, ...] | None = None
    num_ctx: int | None = None
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens < _MIN_OUTPUT_TOKENS:
            object.__setattr__(self, "max_output_tokens", _MIN_OUTPUT_TOKENS)
        if isinstance(self.stop, str):
            object.__setattr__(self, "stop", (self.stop,))
        elif self.stop is not None and not isinstance(self.stop, tuple):
            object.__setattr__(self, "stop", tuple(self.stop))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "stop": list(self.stop) if self.stop is not None else None,
            "num_ctx": self.num_ctx,
            "timeout_s": self.timeout_s,
        }


@dataclass(frozen=True)
class ResolvedGeneration:
    """A configuration plus the decisions that produced it."""

    config: GenerationConfig
    provenance: dict[str, str] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    purpose: str = "answer"
    provider: str = ""
    model: str = ""

    def explain(self) -> str:
        pieces = [
            f"{self.purpose} uses {self.config.max_output_tokens} output tokens",
            f"(provider={self.provider or 'default'}, model={self.model or 'default'})",
        ]
        if self.provenance:
            pieces.append("; ".join(f"{key}={layer}" for key, layer in self.provenance.items()))
        if self.dropped:
            pieces.append("dropped=" + ",".join(sorted(self.dropped)))
        return " ".join(pieces)

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "config": self.config.to_dict(),
            "provenance": dict(self.provenance),
            "dropped": dict(self.dropped),
        }


def _as_number(value: Any, name: str) -> Any:
    if value is None:
        return None
    try:
        parsed = float(value) if name in {"temperature", "top_p", "timeout_s"} else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid generation override for {name}: {value!r}") from exc
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"invalid non-finite generation override for {name}")
    return parsed


def _unsupported_fields(provider: str) -> dict[str, str]:
    name = (provider or "").lower()
    if name == "ollama":
        return {}
    if name in {"gemini", "anthropic"}:
        return {"num_ctx": f"unsupported by {name}"}
    return {
        "top_k": "unsupported by OpenAI-compatible providers",
        "num_ctx": "unsupported by OpenAI-compatible providers",
    }


def resolve_generation(
    purpose: str,
    *,
    mode: str = "auto",
    workflow: str = "agentic",
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    override: Mapping[str, Any] | None = None,
) -> ResolvedGeneration:
    """Resolve one request using purpose, policy, user and safety layers.

    Precedence, lowest to highest: the purpose's configured budget, mode,
    workflow, the user's temperature, a per-request override, then the
    process-wide ``MAX_TOKENS`` ceiling. The ceiling is not an override: it is
    what someone lowers when their context is small, and nothing above it may
    raise it back. Provider limits are not guessed from a model's name; a
    provider that rejects a value reports it and the call fails visibly.
    """
    normalized_purpose = purpose.strip().lower()
    if normalized_purpose not in PURPOSES:
        raise ValueError(f"unknown generation purpose: {purpose!r}")
    normalized_mode = (mode or "auto").strip().lower()
    normalized_workflow = (workflow or "agentic").strip().lower()
    provider_name = (provider or "").strip().lower()
    model_name = (model or "").strip()

    values: dict[str, Any] = {
        "max_output_tokens": settings.output_budget(normalized_purpose),
        "temperature": settings.TEMPERATURE if temperature is None else temperature,
        "top_p": None,
        "top_k": None,
        "stop": None,
        "num_ctx": settings.LLM_NUM_CTX or None,
        "timeout_s": settings.LLM_REQUEST_TIMEOUT,
    }
    provenance = dict.fromkeys(values, "purpose default")

    if normalized_mode == "deep" and normalized_purpose in _DEEP_SCALED:
        values["max_output_tokens"] = int(values["max_output_tokens"] * _DEEP_SCALE)
        provenance["max_output_tokens"] = "mode"

    if normalized_workflow in {"direct", "inspect"} and normalized_purpose == "answer":
        capped = min(values["max_output_tokens"], _DIRECT_ANSWER_CAP)
        if capped != values["max_output_tokens"]:
            values["max_output_tokens"] = capped
            provenance["max_output_tokens"] = "workflow"

    if temperature is not None:
        values["temperature"] = temperature
        provenance["temperature"] = "user configuration"

    for key, raw_value in (override or {}).items():
        if key not in values or raw_value is None:
            continue
        values[key] = raw_value
        provenance[key] = "request override"

    for key in ("max_output_tokens", "temperature", "top_p", "top_k", "timeout_s"):
        values[key] = _as_number(values[key], key)
    if values["stop"] is not None:
        stop_values = (values["stop"],) if isinstance(values["stop"], str) else values["stop"]
        values["stop"] = tuple(str(item) for item in stop_values)

    before_safe_clamp = int(values["max_output_tokens"])
    values["max_output_tokens"] = max(
        _MIN_OUTPUT_TOKENS,
        min(int(values["max_output_tokens"]), int(settings.MAX_TOKENS)),
    )
    if values["max_output_tokens"] != before_safe_clamp:
        provenance["max_output_tokens"] = "system-safe clamp"

    config = GenerationConfig(**values)
    dropped = {
        field: reason
        for field, reason in _unsupported_fields(provider_name).items()
        if getattr(config, field) is not None
    }
    return ResolvedGeneration(
        config=config,
        provenance=provenance,
        dropped=dropped,
        purpose=normalized_purpose,
        provider=provider_name,
        model=model_name,
    )
