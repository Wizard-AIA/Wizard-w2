"""Shared FastAPI dependencies: session resolution, auth and rate limiting."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Header, HTTPException, Query, Request, WebSocket

from src.config import settings
from src.core.session import Session, session_manager
from src.utils.logging import logger


SESSION_HEADER = "X-Session-Id"


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """No-op unless ``API_KEY`` is configured.

    Local-first deployments stay open by default; anyone exposing the service
    beyond localhost can set a key without touching code. Comparison is constant
    time so the key cannot be recovered by timing.
    """
    if not settings.API_KEY:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def get_session(x_session_id: str | None = Header(default=None, alias=SESSION_HEADER)) -> Session:
    """Resolves the caller's session, creating one when absent or expired.

    Header only. A session id is a bearer credential -- whoever presents it
    gets the workspace, datasets and chat history behind it -- and a query
    string is routinely captured in proxy access logs, browser history and
    the Referer header in a way a request header is not. The two routes that
    serve a direct navigation target (a download link a browser tab opens
    without JS setting a header) use :func:`get_session_for_link` instead.
    """
    return session_manager.get_or_create(x_session_id)


def require_session(x_session_id: str | None = Header(default=None, alias=SESSION_HEADER)) -> Session:
    """Like :func:`get_session` but rejects an unknown id instead of silently creating one."""
    session = session_manager.get(x_session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found or expired. Create a new session and re-upload your data.",
        )
    return session


def get_session_for_link(
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
    session_query: str | None = Query(default=None, alias="session"),
) -> Session:
    """Like :func:`require_session`, but also accepts the id as a query param.

    Reserved for routes a browser tab opens directly -- a download link or an
    ``<img>``/``<a>`` target -- where there is no request in flight that could
    carry a custom header. Keeping this accepted only on those routes, rather
    than on every route as before, keeps the leakage surface a session id
    carried in a URL creates (proxy logs, browser history, Referer) limited to
    the two places it is actually unavoidable.
    """
    session_id = x_session_id or session_query
    session = session_manager.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Session not found or expired. Create a new session and re-upload your data.",
        )
    return session


def require_dataset(x_session_id: str | None = Header(default=None, alias=SESSION_HEADER)) -> Session:
    """Resolves a session that must already have an active dataset."""
    session = get_session(x_session_id)
    if not session.has_data:
        raise HTTPException(
            status_code=412,
            detail="No dataset loaded. Upload a file before running an analysis.",
        )
    return session


class SlidingWindowRateLimiter:
    """Fixed-memory sliding window keyed by client address.

    The previous limiter kept an unbounded ``defaultdict(list)`` that was never
    swept, so every IP that ever connected stayed resident for the process
    lifetime. This uses bounded deques and evicts idle keys.
    """

    def __init__(self, max_requests: int, window_seconds: int, max_keys: int = 4096):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self.max_requests:
                return False

            bucket.append(now)

            if len(self._hits) > self.max_keys:
                self._evict_idle(cutoff)
            return True

    def _evict_idle(self, cutoff: float):
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] < cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) > self.max_keys:
            # Still over budget: drop the least recently active keys.
            ordered = sorted(self._hits.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
            for key, _ in ordered[: len(self._hits) - self.max_keys]:
                del self._hits[key]

    def reset(self):
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(
    max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
)

#: A coarser ceiling keyed on the raw address alone. `rate_limiter` above is
#: keyed per (address, session) for fairness among sessions sharing an
#: address; on its own that would let an attacker multiply their effective
#: rate arbitrarily by minting a fresh session id per request. This bounds
#: the address regardless of how many sessions it claims.
ip_rate_limiter = SlidingWindowRateLimiter(
    max_requests=settings.RATE_LIMIT_MAX_REQUESTS * settings.RATE_LIMIT_IP_BURST_MULTIPLIER,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
)


class ConnectionGate:
    """Caps simultaneous WebSocket connections per client.

    HTTP middleware never sees the WebSocket scope, so the old limiter listed
    ``/ws/chat`` in its path set but could not enforce anything there.
    """

    def __init__(self, max_per_key: int):
        self.max_per_key = max_per_key
        self._active: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def acquire(self, key: str) -> bool:
        with self._lock:
            if self._active[key] >= self.max_per_key:
                logger.warning("WebSocket connection rejected, per-client limit reached", client=key)
                return False
            self._active[key] += 1
            return True

    def release(self, key: str):
        with self._lock:
            if self._active.get(key):
                self._active[key] -= 1
                if self._active[key] <= 0:
                    del self._active[key]


#: Fairness among sessions sharing an address. `ws_ip_gate` below is the
#: ceiling on the address itself, for the same reason `ip_rate_limiter` is.
ws_gate = ConnectionGate(settings.WS_MAX_CONCURRENT_PER_IP)
ws_ip_gate = ConnectionGate(settings.WS_MAX_CONCURRENT_PER_IP * settings.RATE_LIMIT_IP_BURST_MULTIPLIER)


def _resolved_ip(peer_host: str | None, forwarded_for: str | None) -> str:
    """The caller's IP, honouring ``X-Forwarded-For`` only from a trusted peer.

    A header is self-reported: without restricting it to a configured proxy,
    any client could claim an arbitrary address to evade its own limit or
    collide someone else's bucket.

    A trusted proxy is expected to *append* the address it received the
    request from as the last hop, not replace the header outright -- so the
    last entry is what the proxy itself observed. The first entry is
    whatever the client sent and is exactly what a client behind that proxy
    could forge by prepending fake addresses ahead of its own.
    """
    trusted = {ip.strip() for ip in settings.FORWARDED_ALLOW_IPS.split(",") if ip.strip()}
    if peer_host and peer_host in trusted and forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return peer_host or "unknown"


def client_ip(request: Request) -> str:
    """The caller's resolved address alone, with no session mixed in."""
    return _resolved_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"))


def client_key(request: Request) -> str:
    """Composite rate-limit key: resolved IP plus session.

    IP alone collides every user behind the same NAT gateway or reverse proxy
    onto one bucket -- one busy session then exhausts the limit for everyone
    else sharing that address. The session id, sent on every mutating request
    this limiter guards, tells them apart without trusting a client-controlled
    header alone.

    This key alone is not a rate limit on the *address* -- a session id is
    itself client-supplied, so a caller minting a fresh one per request would
    get a fresh bucket per request. Callers must also check `ip_rate_limiter`
    against :func:`client_ip`, which bounds the address regardless of how
    many sessions it claims.
    """
    ip = client_ip(request)
    session_id = request.headers.get(SESSION_HEADER) or ""
    return f"{ip}:{session_id}" if session_id else ip


def ws_client_ip(websocket: WebSocket) -> str:
    """Like :func:`client_ip`, for the WebSocket handshake (no ``Request``)."""
    return _resolved_ip(websocket.client.host if websocket.client else None, websocket.headers.get("x-forwarded-for"))


def ws_client_key(websocket: WebSocket) -> str:
    """Like :func:`client_key`, for the WebSocket handshake (no ``Request``)."""
    ip = ws_client_ip(websocket)
    session_id = websocket.query_params.get("session") or websocket.headers.get(SESSION_HEADER.lower()) or ""
    return f"{ip}:{session_id}" if session_id else ip
