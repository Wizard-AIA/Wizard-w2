"""An idempotency key identifies one request, not merely one caller string."""

from __future__ import annotations

import pytest

from src.core.infra.idempotency import IdempotencyConflict, IdempotencyStore, request_fingerprint


def test_cached_result_requires_the_original_request_fingerprint() -> None:
    store = IdempotencyStore()
    original = request_fingerprint(session_id="s1", message="count rows", mode="auto", approved_plan=None)
    changed = request_fingerprint(session_id="s1", message="delete rows", mode="auto", approved_plan=None)
    store.store_result("caller-key", original, {"status": "completed"})

    assert store.get_cached("caller-key", original) == {"status": "completed"}
    with pytest.raises(IdempotencyConflict, match="different request"):
        store.get_cached("caller-key", changed)


def test_fingerprint_is_scoped_to_the_session_and_all_turn_inputs() -> None:
    baseline = request_fingerprint(session_id="s1", message="count", mode="auto", approved_plan=None)
    assert baseline != request_fingerprint(session_id="s2", message="count", mode="auto", approved_plan=None)
    assert baseline != request_fingerprint(session_id="s1", message="count", mode="fast", approved_plan=None)
    assert baseline != request_fingerprint(session_id="s1", message="count", mode="auto", approved_plan="approved")
