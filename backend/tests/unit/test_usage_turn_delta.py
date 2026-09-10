"""A completed turn must not be charged for earlier session work."""

from __future__ import annotations

from src.core.llm.usage import TokenUsage, UsageLedger


def test_turn_delta_excludes_preexisting_session_usage_and_includes_new_subagent_usage() -> None:
    ledger = UsageLedger()
    ledger.record("parent", "openai", "gpt-4o-mini", "manager", TokenUsage(100, 10))
    before = ledger.snapshot_many(["parent"])

    ledger.record("parent", "openai", "gpt-4o-mini", "worker", TokenUsage(20, 5))
    ledger.record("parent:branch:1", "openai", "gpt-4o-mini", "worker", TokenUsage(30, 15))
    delta = ledger.totals_since(before, ["parent", "parent:branch:1"])

    assert delta["calls"] == 2
    assert delta["input_tokens"] == 50
    assert delta["output_tokens"] == 20
    assert delta["total_tokens"] == 70
    assert {record["role"] for record in delta["records"]} == {"worker"}


def test_turn_delta_is_empty_when_no_call_followed_the_snapshot() -> None:
    ledger = UsageLedger()
    ledger.record("session", "openai", "gpt-4o-mini", "manager", TokenUsage(10, 2))

    assert ledger.totals_since(ledger.snapshot_many(["session"]), ["session"])["calls"] == 0
