"""What a turn cost, in tokens and — where it is knowable — in money.

A local turn costs electricity and a cloud turn costs money, and until now the
app could not tell you either. Cost is surfaced the way the grounding layer
surfaces numbers: measured where the provider reports it, marked as an estimate
where it does not, and **absent rather than guessed** where the price is unknown.
A fabricated dollar figure is worse than no dollar figure.

Under `local-only` there is no meter at all. Rendering "$0.00" implies a number
that was computed; the honest statement is that nothing left the machine.

The ledger is keyed by session id rather than held on the session object, so
``core/llm`` does not have to import ``core.session`` — the same reason the model
registry keeps its own cache.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.providers import is_cloud


#: USD per million tokens, (input, output), for models whose price is published.
#: Prefix match on the model id, longest first, because providers version their
#: names (`claude-sonnet-4-5-20250929`). A model absent from this table reports
#: tokens and no cost — see the module docstring.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-haiku": (0.25, 1.25),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-3.5-turbo": (0.5, 1.5),
    "o3-mini": (1.1, 4.4),
    "o1-mini": (1.1, 4.4),
    "o1": (15.0, 60.0),
}

#: Characters per token when nothing better is available. Crude, and every
#: record built this way is flagged `exact=False` so the UI can say so.
CHARS_PER_TOKEN = 4


def price_for(model: str) -> tuple[float, float] | None:
    """Published (input, output) USD per million tokens, or ``None`` if unknown."""
    name = (model or "").strip().lower()
    if not name:
        return None
    for prefix in sorted(PRICING, key=len, reverse=True):
        if name.startswith(prefix) or prefix in name:
            return PRICING[prefix]
    return None


@dataclass(frozen=True)
class TokenUsage:
    """Tokens for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    #: False when the numbers came from a character-count estimate rather than
    #: from the provider.
    exact: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _coerce(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _from_metadata(response: Any) -> TokenUsage | None:
    """Reads whichever shape the installed client version reports."""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and (usage.get("input_tokens") or usage.get("output_tokens")):
        return TokenUsage(_coerce(usage.get("input_tokens")), _coerce(usage.get("output_tokens")))

    meta = getattr(response, "response_metadata", None)
    if isinstance(meta, dict):
        token_usage = meta.get("token_usage") or meta.get("usage")
        if isinstance(token_usage, dict):
            given = TokenUsage(
                _coerce(token_usage.get("prompt_tokens") or token_usage.get("input_tokens")),
                _coerce(token_usage.get("completion_tokens") or token_usage.get("output_tokens")),
            )
            if given.total_tokens:
                return given
        # Ollama reports its own counts at the top level.
        if meta.get("prompt_eval_count") or meta.get("eval_count"):
            return TokenUsage(_coerce(meta.get("prompt_eval_count")), _coerce(meta.get("eval_count")))
    return None


def extract_usage(response: Any, prompt: str = "", text: str = "") -> TokenUsage:
    """Token counts for one call, degrading to an estimate rather than to nothing.

    Written against three shapes on purpose: the reported counts differ between
    langchain-ollama, langchain-openai and langchain-anthropic, and between
    versions of each, and a meter that silently reads zero is worse than one that
    admits it approximated.
    """
    found = _from_metadata(response)
    if found is not None:
        return found
    return TokenUsage(
        input_tokens=len(prompt) // CHARS_PER_TOKEN,
        output_tokens=len(text) // CHARS_PER_TOKEN,
        exact=False,
    )


@dataclass
class UsageRecord:
    """Everything spent on one (provider, model, role) in one session."""

    provider: str
    model: str
    role: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        """Estimated spend, or ``None`` when this model is not billed or not priced."""
        if not is_cloud(self.provider):
            return None
        price = price_for(self.model)
        if price is None:
            return None
        return (self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        cost = self.cost_usd
        return {
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": None if cost is None else round(cost, 6),
            "estimated": self.estimated,
            "cloud": is_cloud(self.provider),
        }


@dataclass
class SessionUsage:
    """One session's records, and the totals a UI actually renders."""

    records: dict[tuple[str, str, str], UsageRecord] = field(default_factory=dict)

    def add(self, provider: str, model: str, role: str, usage: TokenUsage) -> UsageRecord:
        key = (provider, model, role)
        record = self.records.get(key)
        if record is None:
            record = UsageRecord(provider=provider, model=model, role=role)
            self.records[key] = record
        record.calls += 1
        record.input_tokens += usage.input_tokens
        record.output_tokens += usage.output_tokens
        record.estimated = record.estimated or not usage.exact
        return record

    def copy(self) -> SessionUsage:
        """An immutable-at-the-call-site snapshot of this aggregate."""
        return SessionUsage(
            records={
                key: UsageRecord(
                    provider=record.provider,
                    model=record.model,
                    role=record.role,
                    calls=record.calls,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    estimated=record.estimated,
                )
                for key, record in self.records.items()
            }
        )

    def subtract(self, earlier: SessionUsage) -> SessionUsage:
        """Return the non-negative delta from a prior snapshot."""
        delta = SessionUsage()
        for key, current in self.records.items():
            previous = earlier.records.get(key)
            calls = current.calls - (previous.calls if previous else 0)
            input_tokens = current.input_tokens - (previous.input_tokens if previous else 0)
            output_tokens = current.output_tokens - (previous.output_tokens if previous else 0)
            if calls <= 0 and input_tokens <= 0 and output_tokens <= 0:
                continue
            delta.records[key] = UsageRecord(
                provider=current.provider,
                model=current.model,
                role=current.role,
                calls=max(0, calls),
                input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens),
                estimated=current.estimated or (previous.estimated if previous else False),
            )
        return delta

    def to_dict(self) -> dict[str, Any]:
        records = [record.to_dict() for record in self.records.values()]
        priced = [record for record in self.records.values() if record.cost_usd is not None]
        unpriced_cloud = [
            record for record in self.records.values() if is_cloud(record.provider) and record.cost_usd is None
        ]
        any_cloud = any(is_cloud(record.provider) for record in self.records.values())
        return {
            "records": records,
            "calls": sum(record.calls for record in self.records.values()),
            "input_tokens": sum(record.input_tokens for record in self.records.values()),
            "output_tokens": sum(record.output_tokens for record in self.records.values()),
            "total_tokens": sum(record.total_tokens for record in self.records.values()),
            # None, not 0.0, when nothing billable ran: the difference between
            # "no spend" and "spend we could not price" has to survive to the UI.
            "cost_usd": round(sum(record.cost_usd or 0.0 for record in priced), 6) if priced else None,
            "any_cloud": any_cloud,
            "estimated": any(record.estimated for record in self.records.values()),
            #: Cloud models whose price is not published, named so the readout can
            #: say which ones it could not cost rather than quietly under-reporting.
            "unpriced_models": sorted({record.model for record in unpriced_cloud}),
        }


class UsageLedger:
    """Per-session usage, keyed by id so ``core/llm`` need not import the session."""

    def __init__(self):
        self._sessions: dict[str, SessionUsage] = defaultdict(SessionUsage)
        self._lock = threading.Lock()

    def record(self, session_id: str | None, provider: str, model: str, role: str, usage: TokenUsage) -> None:
        if not session_id or not usage.total_tokens:
            return
        with self._lock:
            self._sessions[session_id].add(provider, model or "unknown", role, usage)

    def totals(self, session_id: str | None) -> dict[str, Any]:
        with self._lock:
            usage = self._sessions.get(session_id or "")
            return usage.to_dict() if usage else SessionUsage().to_dict()

    def totals_many(self, session_ids: list[str]) -> dict[str, Any]:
        """Combined totals across a parent session and any of its subagents.

        Each subagent's LLM calls book under its own composite session id -- a
        separate bucket created automatically because `SubagentSession.id` is
        that composite id -- so the parent's own `totals()` alone would
        under-report what a turn with subagents actually cost. Records are
        merged by `(provider, model, role)`: a subagent's `worker` calls fold
        into the same bucket the main loop's `worker` calls do, which is
        correct for "what did this turn cost." The per-branch breakdown a UI
        wants instead is still available from `totals(child_id)` individually.
        """
        with self._lock:
            return self._combined_locked(session_ids).to_dict()

    def snapshot_many(self, session_ids: list[str]) -> SessionUsage:
        """Capture aggregate usage before a turn begins without exposing live state."""
        with self._lock:
            return self._combined_locked(session_ids)

    def totals_since(self, snapshot: SessionUsage, session_ids: list[str]) -> dict[str, Any]:
        """Usage added by this turn, including all subagents it created."""
        with self._lock:
            return self._combined_locked(session_ids).subtract(snapshot).to_dict()

    def _combined_locked(self, session_ids: list[str]) -> SessionUsage:
        merged = SessionUsage()
        unique_ids = [session_id for session_id in dict.fromkeys(session_ids) if session_id]
        for session_id in unique_ids:
            usage = self._sessions.get(session_id)
            if usage is None:
                continue
            for key, record in usage.records.items():
                target = merged.records.get(key)
                if target is None:
                    merged.records[key] = UsageRecord(
                        provider=record.provider,
                        model=record.model,
                        role=record.role,
                        calls=record.calls,
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        estimated=record.estimated,
                    )
                else:
                    target.calls += record.calls
                    target.input_tokens += record.input_tokens
                    target.output_tokens += record.output_tokens
                    target.estimated = target.estimated or record.estimated
        return merged

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


usage_ledger = UsageLedger()


__all__ = [
    "CHARS_PER_TOKEN",
    "PRICING",
    "SessionUsage",
    "TokenUsage",
    "UsageLedger",
    "UsageRecord",
    "extract_usage",
    "price_for",
    "usage_ledger",
]
