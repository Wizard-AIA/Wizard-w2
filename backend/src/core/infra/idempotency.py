"""Idempotency-key store for preventing duplicate request execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any


class IdempotencyStore:
    """In-memory idempotency key store with TTL.

    Tracks in-flight request keys and caches their results to prevent
    duplicate execution of the same request.
    """

    def __init__(self, ttl: float = 300.0):
        self._ttl = ttl
        self._results: dict[str, tuple[float, str, Any]] = {}  # key -> (expires_at, fingerprint, result)
        self._locks: dict[str, asyncio.Lock] = {}  # key -> in-flight lock

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _, _) in self._results.items() if now > exp]
        for k in expired:
            self._results.pop(k, None)
            self._locks.pop(k, None)

    def get_cached(self, key: str, fingerprint: str) -> Any | None:
        self._prune()
        entry = self._results.get(key)
        if entry and time.monotonic() <= entry[0]:
            if entry[1] != fingerprint:
                raise IdempotencyConflict("Idempotency key was already used with a different request.")
            return entry[2]
        return None

    def acquire_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def store_result(self, key: str, fingerprint: str, result: Any) -> None:
        self._results[key] = (time.monotonic() + self._ttl, fingerprint, result)

    def release(self, key: str) -> None:
        self._locks.pop(key, None)


class IdempotencyConflict(ValueError):
    """A caller attempted to reuse a key for a non-identical request."""


def request_fingerprint(*, session_id: str, message: str, mode: str, approved_plan: str | None) -> str:
    """Stable digest of every request field that changes a turn's side effects."""
    payload = json.dumps(
        {"session_id": session_id, "message": message, "mode": mode, "approved_plan": approved_plan},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store
