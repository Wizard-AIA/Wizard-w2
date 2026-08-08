"""FastAPI application assembly."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.deps import client_key, rate_limiter
from src.api.routes import chat, connections, datasets, export, meta, sandbox, sessions, skills, workspace
from src.config import settings
from src.core.embeddings import embedding_service
from src.core.infra.queue import get_queue
from src.core.llm import llm_provider
from src.core.session import session_manager
from src.core.tools import runtime as runtime_backend
from src.core.tools.host_runtime import host_runtime_pool
from src.core.tools.sandbox import sandbox_pool
from src.utils.hostinfo import host_info
from src.utils.logging import configure_logger, logger


configure_logger()

MAINTENANCE_INTERVAL_SECONDS = 300

# Paths whose cost justifies rate limiting. Read-only routes are excluded so a
# polling UI is never throttled.
# `/api/connections` is here because an import reads a remote table, which is at
# least as expensive as an upload -- and unlike an upload, the cost lands on
# somebody else's database too.
RATE_LIMITED_PREFIXES = ("/api/chat", "/api/datasets", "/api/connections")

#: Methods a same-origin form or fetch can use to change state. GET is
#: excluded on purpose -- it must stay side-effect-free for this check to mean
#: anything.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


async def _maintenance_loop():
    """Periodically reaps idle sessions and finished jobs."""
    while True:
        try:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            reaped = await asyncio.to_thread(session_manager.reap_expired)
            pruned = get_queue().prune()
            if reaped or pruned:
                logger.info("Maintenance sweep", sessions_reaped=reaped, jobs_pruned=pruned)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("Maintenance sweep failed", error=str(exc))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "Starting Wizard backend",
        env=settings.ENV,
        provider=settings.API_PROVIDER,
        # Sized from the machine at boot, so the log is the record of what it
        # decided -- "why is it only using four threads" has an answer here.
        execution_backend=runtime_backend.active_backend(),
        profile=settings.system_profile,
        cores=host_info().cores,
        ram_gb=None if host_info().ram_gb is None else round(host_info().ram_gb, 1),
        inference_threads=settings.LLM_NUM_THREAD,
        sandbox_mem_limit=settings.SANDBOX_MEM_LIMIT,
        max_sessions=settings.SESSION_MAX_ACTIVE,
    )
    # Containers left behind by a previous process would otherwise accumulate.
    await asyncio.to_thread(sandbox_pool.prune_orphans)

    # Resolve the embedding encoder now, on a background thread, so no question
    # pays for a cold model load. Lazily, the first one did -- and the load
    # overran the request timeout, so a provider that embeds in 50ms once
    # resident was written off and retrieval silently degraded to lexical.
    if settings.EMBEDDINGS_WARM_ON_STARTUP:
        embedding_service.warm()

    # Same reasoning, for the manager and worker: resolve them now so the
    # first real question does not pay for a cold model load mid-answer.
    if settings.LLM_WARM_ON_STARTUP:
        llm_provider.warm()

    task = asyncio.ensure_future(_maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await task
        await get_queue().shutdown()
        await asyncio.to_thread(session_manager.shutdown)
        await asyncio.to_thread(sandbox_pool.shutdown)
        await asyncio.to_thread(host_runtime_pool.shutdown)
        logger.info("Wizard backend stopped")


app = FastAPI(
    title="Wizard w2",
    description="Local-first autonomous data analysis agent.",
    version=meta.API_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def observability_and_limits(request: Request, call_next):
    """Logs each request, blocks cross-site form submissions, and rate-limits.

    When ``API_KEY`` is unset -- the default for a local-first install --
    mutating routes rely on CORS alone, and CORS does not stop a simple
    cross-site request: a plain HTML form on a page the user has open in
    another tab can still POST to this API, because a form submission is not
    subject to CORS's preflight check. A browser does attach an ``Origin``
    header to that request, though, so mutating requests carrying one that
    does not match an allowed origin are refused. A request with no ``Origin``
    at all -- a script, curl, the CLI -- is not this attack and is let
    through; ``API_KEY`` is the control for that case.
    """
    path = request.url.path

    if (
        not settings.API_KEY
        and request.method in MUTATING_METHODS
        and path.startswith("/api/")
    ):
        origin = request.headers.get("origin")
        if origin and origin not in settings.cors_origins:
            logger.warning("Blocked cross-site request", path=path, origin=origin)
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site request blocked. Set API_KEY to allow requests from other origins."},
            )

    if any(path.startswith(prefix) for prefix in RATE_LIMITED_PREFIXES) and request.method in {
        "POST",
        "PUT",
        "DELETE",
    }:
        if not rate_limiter.allow(client_key(request)):
            logger.warning("Rate limit exceeded", path=path, client=client_key(request))
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a moment and try again."},
            )

    started = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - started

    logger.info(
        "Request processed",
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_ms=round(duration * 1000, 2),
    )
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Credentials cannot be combined with a wildcard origin; the setting object
    # resolves the two together so the combination is never invalid.
    allow_credentials=settings.cors_allow_credentials,
    # PUT is here because `PUT /api/data-mode/dataset/{name}` is what the
    # per-source data policy control calls, cross-origin, from the frontend. It
    # was missing, so that control's preflight failed in a browser while every
    # test passed -- TestClient makes no preflight request.
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


app.include_router(meta.router)
app.include_router(sessions.router)
app.include_router(datasets.router)
app.include_router(connections.router)
app.include_router(workspace.router)
app.include_router(skills.router)
app.include_router(sandbox.router)
app.include_router(sandbox.jobs_router)
app.include_router(chat.router)
app.include_router(export.router)


__all__ = ["app"]
