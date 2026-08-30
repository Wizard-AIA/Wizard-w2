"""Background job queue with bounded concurrency.

Why this exists
---------------
Uploading a dataset used to run semantic cleaning inline inside the ``/upload``
request: an LLM round-trip plus a container restart, all before the HTTP response
was written. Large files made that a multi-minute request that any proxy would
time out.

Jobs are now submitted here and the client polls ``/jobs/{id}`` or watches the
WebSocket. Concurrency is capped so a laptop running local models is not asked to
service ten simultaneous inference jobs.

The default backend is an in-process asyncio worker pool -- no extra services.
When ``REDIS_URL`` is set the job *state* is mirrored into Redis so status
survives a reload and can be read by another worker; execution still happens in
this process, which keeps the local-first promise intact.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from src.config import settings
from src.core.infra.cache import get_cache
from src.utils.logging import logger


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    session_id: str | None = None
    max_retries: int = 3
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


JobHandler = Callable[["Job"], Awaitable[Any]]


class JobQueue:
    """Async worker pool with a bounded number of concurrent jobs."""

    def __init__(self, max_workers: int | None = None):
        self.max_workers = max_workers or settings.QUEUE_MAX_WORKERS
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._cache = get_cache()

    # ------------------------------------------------------------------ #
    def _get_semaphore(self) -> asyncio.Semaphore:
        # Created lazily so the queue can be constructed at import time, outside a loop.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_workers)
        return self._semaphore

    def _persist(self, job: Job):
        with self._lock:
            self._jobs[job.id] = job
        try:
            self._cache.set(f"job:{job.id}", job.to_dict(), ttl=settings.JOB_RESULT_TTL_SECONDS)
        except Exception as exc:  # cache is best-effort
            logger.debug("Job state not mirrored to cache", job_id=job.id, error=str(exc))

    # ------------------------------------------------------------------ #
    def submit(self, kind: str, handler: JobHandler, session_id: str | None = None) -> Job:
        """Schedules ``handler`` and returns immediately with a PENDING job."""
        job = Job(id=uuid.uuid4().hex[:16], kind=kind, session_id=session_id)
        self._persist(job)

        async def runner():
            async with self._get_semaphore():
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                self._persist(job)
                logger.info("Job started", job_id=job.id, kind=kind)
                while True:
                    try:
                        job.result = await handler(job)
                        job.status = JobStatus.SUCCEEDED
                        job.progress = 1.0
                        break
                    except asyncio.CancelledError:
                        job.status = JobStatus.CANCELLED
                        job.error = "Cancelled"
                        self._persist(job)
                        raise
                    except Exception as exc:
                        job.retry_count += 1
                        if job.retry_count <= job.max_retries:
                            delay = (2**job.retry_count) + random.uniform(0, 0.5)
                            logger.warning(
                                "job_retry",
                                job_id=job.id,
                                kind=kind,
                                retry=job.retry_count,
                                delay_s=round(delay, 2),
                                error=str(exc),
                            )
                            await asyncio.sleep(delay)
                            continue
                        job.status = JobStatus.FAILED
                        job.error = str(exc)
                        logger.error("job_failed_permanently", job_id=job.id, kind=kind, error=str(exc))
                        self._route_to_dlq(job, exc)
                        break
                job.finished_at = time.time()
                self._persist(job)
                self._tasks.pop(job.id, None)

        task = asyncio.ensure_future(runner())
        self._tasks[job.id] = task
        return job

    def _route_to_dlq(self, job: Job, exc: Exception) -> None:
        """Persist a terminally failed job to the dead-letter queue."""
        import traceback

        from src.core.database import db_mgr

        try:
            db_mgr.save_dead_letter(
                job_id=job.id,
                kind=job.kind if hasattr(job, "kind") else "unknown",
                payload=None,
                error=str(exc),
                stack_trace=traceback.format_exc(),
                retry_count=job.retry_count,
            )
        except Exception as dlq_exc:
            logger.error("dlq_persist_failed", job_id=job.id, error=str(dlq_exc))

    async def run_now(self, kind: str, handler: JobHandler, session_id: str | None = None) -> Job:
        """Runs a job to completion while still respecting the concurrency cap."""
        job = self.submit(kind, handler, session_id=session_id)
        task = self._tasks.get(job.id)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("Job cancelled while awaited by run_now", job=job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job

        payload = self._cache.get(f"job:{job_id}")
        if isinstance(payload, dict):
            try:
                payload = dict(payload)
                payload["status"] = JobStatus(payload.get("status", "pending"))
                return Job(**payload)
            except (TypeError, ValueError):
                return None
        return None

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def list_jobs(self, session_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if session_id:
            jobs = [j for j in jobs if j.session_id == session_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def prune(self, max_age_seconds: int | None = None) -> int:
        """Drops finished jobs older than the TTL. Returns how many were removed."""
        ttl = max_age_seconds or settings.JOB_RESULT_TTL_SECONDS
        cutoff = time.time() - ttl
        removed = 0
        with self._lock:
            for job_id in [
                jid
                for jid, job in self._jobs.items()
                if job.is_terminal and (job.finished_at or job.created_at) < cutoff
            ]:
                del self._jobs[job_id]
                removed += 1
        return removed

    async def shutdown(self):
        """Cancels in-flight work; used on application shutdown."""
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    @property
    def backend_name(self) -> str:
        return "redis-backed" if settings.redis_enabled else "in-process"


InMemoryJobQueue = JobQueue


class RedisJobQueue(JobQueue):
    """External Redis queue backend."""

    def __init__(self, max_workers: int | None = None):
        super().__init__(max_workers)
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        self.queue_prefix = getattr(settings, "REDIS_KEY_PREFIX", "wizard:")
        self.queue_key = f"{self.queue_prefix}jobs:queue"
        self._handlers: dict[str, Any] = {}
        self._worker_tasks: list[asyncio.Task[Any]] = []
        self._worker_started = False

    def _persist_to_redis(self, job: Job):
        try:
            asyncio.ensure_future(self.redis.hset(f"{self.queue_prefix}jobs:{job.id}", mapping=job.to_dict()))
            asyncio.ensure_future(
                self.redis.expire(f"{self.queue_prefix}jobs:{job.id}", settings.JOB_RESULT_TTL_SECONDS)
            )
        except Exception as exc:
            logger.debug("Failed to persist job to Redis hash", error=str(exc))

    def _start_workers_if_needed(self):
        if not self._worker_started:
            self._worker_started = True
            for _ in range(self.max_workers):
                t = asyncio.ensure_future(self._worker_loop())
                self._worker_tasks.append(t)

    async def _worker_loop(self):
        while True:
            try:
                # Pop job from Redis list
                res = await self.redis.brpop(self.queue_key, timeout=1)
                if not res:
                    continue
                _, job_id = res

                # Retrieve local handler
                handler = self._handlers.pop(job_id, None)
                if not handler:
                    continue

                # Fetch job state
                job = self._jobs.get(job_id)
                if not job:
                    continue

                # Execute
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                self._persist(job)
                self._persist_to_redis(job)

                # Periodic heartbeat task
                async def heartbeat():
                    while job.status == JobStatus.RUNNING:
                        await asyncio.sleep(5)
                        self._persist_to_redis(job)

                hb_task = asyncio.ensure_future(heartbeat())

                try:
                    job.result = await handler(job)
                    job.status = JobStatus.SUCCEEDED
                    job.progress = 1.0
                except asyncio.CancelledError:
                    job.status = JobStatus.CANCELLED
                    job.error = "Cancelled"
                except Exception as exc:
                    job.status = JobStatus.FAILED
                    job.error = str(exc)
                    self._route_to_dlq(job, exc)
                finally:
                    hb_task.cancel()
                    job.finished_at = time.time()
                    self._persist(job)
                    self._persist_to_redis(job)
                    self._tasks.pop(job.id, None)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Redis worker loop error", error=str(exc))
                await asyncio.sleep(1)

    def submit(self, kind: str, handler: JobHandler, session_id: str | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:16], kind=kind, session_id=session_id)
        self._handlers[job.id] = handler
        self._persist(job)
        self._persist_to_redis(job)

        async def _push():
            await self.redis.lpush(self.queue_key, job.id)

        task = asyncio.ensure_future(_push())
        self._tasks[job.id] = task

        self._start_workers_if_needed()
        return job

    async def shutdown(self):
        for t in self._worker_tasks:
            t.cancel()
        await super().shutdown()


_queue: JobQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> JobQueue:
    global _queue
    if _queue is not None:
        return _queue
    with _queue_lock:
        if _queue is None:
            if settings.redis_enabled:
                import importlib.util

                if importlib.util.find_spec("redis.asyncio") is not None:
                    _queue = RedisJobQueue()
                else:
                    _queue = JobQueue()
            else:
                _queue = JobQueue()
            logger.info("Job queue ready", backend=_queue.backend_name, max_workers=_queue.max_workers)
        return _queue


def reset_queue():
    """Test hook."""
    global _queue
    with _queue_lock:
        _queue = None
