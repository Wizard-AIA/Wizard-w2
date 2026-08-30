"""Key-value cache with an optional Redis backend.

Local-first by default: with ``REDIS_URL`` unset the process uses a bounded,
TTL-aware in-memory dict and requires no extra services or packages. Setting
``REDIS_URL`` (and installing ``redis``) swaps in a shared backend so several
workers can cooperate. If Redis is configured but unreachable the cache silently
degrades to in-process rather than taking the app down -- a cache is never worth
an outage.
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

from src.config import settings
from src.utils.logging import logger


class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None:
        raise NotImplementedError

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes | None:
        return None

    def set_bytes(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store raw bytes with optional TTL."""
        return None

    def get_stale(self, key: str) -> tuple[Any | None, bool]:
        return self.get(key), True

    @property
    def name(self) -> str:
        return type(self).__name__


class InProcessCache(CacheBackend):
    """Thread-safe LRU with per-entry expiry."""

    def __init__(self, capacity: int = 512):
        self.capacity = capacity
        self._store: OrderedDict[str, tuple[Any, float | None, int | None]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at, ttl = entry
            if expires_at is not None and expires_at < time.time():
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def get_stale(self, key: str) -> tuple[Any | None, bool]:
        """Return (value, is_fresh). Returns stale values within grace period."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, False
            value, expires_at, ttl = entry
            if expires_at is None:
                self._store.move_to_end(key)
                return value, True
            is_fresh = time.time() < expires_at
            # Grace period: 2x original TTL
            grace_expired = ttl is not None and time.time() > (expires_at + ttl)
            if grace_expired:
                del self._store[key]
                return None, False
            self._store.move_to_end(key)
            return value, is_fresh

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        with self._lock:
            self._store[key] = (value, expires_at, ttl)
            self._store.move_to_end(key)
            while len(self._store) > self.capacity:
                self._store.popitem(last=False)

    def get_bytes(self, key: str) -> bytes | None:
        val = self.get(key)
        return bytes(val) if isinstance(val, (bytes, bytearray)) else None

    def set_bytes(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.set(key, bytes(value), ttl=ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


class RedisCache(CacheBackend):
    """Redis-backed cache. Values are JSON-encoded."""

    def __init__(self, url: str, prefix: str = "wizard:"):
        import redis  # imported lazily; only required when REDIS_URL is set

        self.prefix = prefix
        self._client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=3)
        self._client.ping()

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str) -> Any | None:
        raw = self._client.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, default=str)
        if ttl:
            self._client.setex(self._key(key), ttl, payload)
        else:
            self._client.set(self._key(key), payload)

    def set_bytes(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store raw bytes without JSON serialization."""
        try:
            if ttl:
                self._client.setex(self._key(key), ttl, value)
            else:
                self._client.set(self._key(key), value)
        except Exception:
            pass

    def get_bytes(self, key: str) -> bytes | None:
        """Retrieve raw bytes without JSON deserialization."""
        try:
            return self._client.get(self._key(key))
        except Exception:
            return None

    def delete(self, key: str) -> None:
        self._client.delete(self._key(key))

    def clear(self) -> None:
        for key in self._client.scan_iter(f"{self.prefix}*"):
            self._client.delete(key)


_cache: CacheBackend | None = None
_cache_lock = threading.Lock()


def get_cache() -> CacheBackend:
    """Returns the process-wide cache, constructing it on first use."""
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        if settings.redis_enabled:
            try:
                _cache = RedisCache(settings.REDIS_URL)
                logger.info("Cache backend: Redis", url=settings.REDIS_URL)
                return _cache
            except Exception as exc:
                logger.warning("Redis unavailable, using in-process cache", url=settings.REDIS_URL, error=str(exc))
        _cache = InProcessCache()
        logger.info("Cache backend: in-process")
        return _cache


def reset_cache_backend():
    """Test hook: forces re-resolution on next access."""
    global _cache
    with _cache_lock:
        _cache = None
