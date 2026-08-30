"""Idempotency-key store for preventing duplicate request execution."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.utils.logging import logger


class IdempotencyStore:
    """In-memory idempotency key store with TTL.
    
    Tracks in-flight request keys and caches their results to prevent
    duplicate execution of the same request.
    """

    def __init__(self, ttl: float = 300.0):
        self._ttl = ttl
        self._results: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, result)
        self._locks: dict[str, asyncio.Lock] = {}  # key -> in-flight lock

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._results.items() if now > exp]
        for k in expired:
            self._results.pop(k, None)
            self._locks.pop(k, None)

    def get_cached(self, key: str) -> Any | None:
        self._prune()
        entry = self._results.get(key)
        if entry and time.monotonic() <= entry[0]:
            return entry[1]
        return None

    def acquire_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def store_result(self, key: str, result: Any) -> None:
        self._results[key] = (time.monotonic() + self._ttl, result)

    def release(self, key: str) -> None:
        self._locks.pop(key, None)


_store: IdempotencyStore | None = None


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store
