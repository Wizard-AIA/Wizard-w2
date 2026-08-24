"""Unit tests for the cache, job queue, embeddings and database layers."""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from src.core.database import DatabaseManager
from src.core.embeddings import EmbeddingService, cosine_similarity
from src.core.infra.cache import InProcessCache
from src.core.infra.queue import Job, JobQueue, JobStatus


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def test_cache_round_trip() -> None:
    cache = InProcessCache()
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}


def test_cache_miss_returns_none() -> None:
    assert InProcessCache().get("absent") is None


def test_cache_expires_entries() -> None:
    cache = InProcessCache()
    cache.set("k", "v", ttl=-1)  # already expired
    assert cache.get("k") is None


def test_cache_evicts_least_recently_used() -> None:
    cache = InProcessCache(capacity=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # promotes "a"
    cache.set("c", 3)  # should evict "b"

    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_cache_respects_capacity_under_concurrency() -> None:
    cache = InProcessCache(capacity=50)

    def writer(offset: int) -> None:
        for index in range(100):
            cache.set(f"key-{offset}-{index}", index)

    threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(cache) <= 50


def test_cache_delete_and_clear() -> None:
    cache = InProcessCache()
    cache.set("a", 1)
    cache.delete("a")
    assert cache.get("a") is None

    cache.set("b", 2)
    cache.clear()
    assert cache.get("b") is None


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #
async def test_queue_runs_a_job_to_completion() -> None:
    queue = JobQueue(max_workers=2)

    async def handler(job: Job) -> str:
        return "done"

    job = await queue.run_now("test", handler)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == "done"
    assert job.is_terminal


async def test_queue_records_failure_without_raising() -> None:
    queue = JobQueue(max_workers=1)

    async def handler(job: Job) -> None:
        raise ValueError("boom")

    job = await queue.run_now("failing", handler)
    assert job.status is JobStatus.FAILED
    assert "boom" in (job.error or "")


async def test_queue_enforces_concurrency_limit() -> None:
    queue = JobQueue(max_workers=2)
    concurrent = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(job: Job) -> None:
        nonlocal concurrent, peak
        async with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        await asyncio.sleep(0.02)
        async with lock:
            concurrent -= 1

    jobs = [queue.submit("slow", handler) for _ in range(6)]
    await asyncio.gather(*(asyncio.sleep(0) for _ in jobs))
    while any(not (queue.get(job.id) or job).is_terminal for job in jobs):
        await asyncio.sleep(0.01)

    assert peak <= 2, f"concurrency cap exceeded: {peak}"


async def test_queue_lookup_and_listing() -> None:
    queue = JobQueue(max_workers=1)

    async def handler(job: Job) -> int:
        return 42

    job = await queue.run_now("kind", handler, session_id="s1")
    assert queue.get(job.id) is not None
    assert queue.get("missing") is None
    assert [entry.id for entry in queue.list_jobs(session_id="s1")] == [job.id]
    assert queue.list_jobs(session_id="other") == []


async def test_queue_prunes_finished_jobs() -> None:
    queue = JobQueue(max_workers=1)

    async def handler(job: Job) -> None:
        return None

    job = await queue.run_now("kind", handler)
    job.finished_at = time.time() - 10_000
    assert queue.prune(max_age_seconds=60) == 1
    assert queue.get(job.id) is None or queue.get(job.id).id == job.id


def test_queue_reports_local_backend_by_default() -> None:
    assert JobQueue().backend_name == "in-process"


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
def test_cosine_similarity_bounds() -> None:
    vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)
    assert cosine_similarity(vector, np.array([0.0, 1.0, 0.0], dtype=np.float32)) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "left,right",
    [
        (None, np.array([1.0])),
        (np.array([1.0]), None),
        (np.array([]), np.array([])),
        (np.array([0.0, 0.0]), np.array([1.0, 1.0])),
        (np.array([1.0, 2.0]), np.array([1.0])),  # mismatched dimensions
    ],
)
def test_cosine_similarity_is_total(left, right) -> None:
    """Never raises: retrieval must degrade, not crash, on bad vectors."""
    assert cosine_similarity(left, right) == 0.0


def test_embedding_service_falls_back_without_a_model() -> None:
    """No network, no model: encoding still returns a usable vector."""
    service = EmbeddingService(use_fallback=True)
    vector = service.encode("hello world")

    assert vector.shape == (384,)
    assert not service.is_semantic


def test_hashing_fallback_is_deterministic() -> None:
    service = EmbeddingService(use_fallback=True)
    assert np.allclose(service.encode("same text"), service.encode("same text"))


def test_hashing_fallback_matches_identical_text() -> None:
    service = EmbeddingService(use_fallback=True)
    identical = service.similarity(service.encode("total bill by day"), service.encode("total bill by day"))
    different = service.similarity(service.encode("total bill by day"), service.encode("zzz qqq"))
    assert identical > different


def test_rank_orders_by_similarity() -> None:
    service = EmbeddingService(use_fallback=True)
    ranked = service.rank("average revenue", [("average revenue", None), ("unrelated topic", None)])

    assert ranked[0][1] == 0
    assert ranked[0][0] >= ranked[1][0]


def test_rank_on_empty_candidates() -> None:
    assert EmbeddingService(use_fallback=True).rank("q", []) == []


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def test_database_persists_and_reads_cache_entries(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    embedding = np.ones(8, dtype=np.float32)

    manager.save_cache_entry("query", ["a", "b"], "print(1)", embedding)
    entries = manager.get_cache_entries(["a", "b"])

    assert len(entries) == 1
    assert entries[0]["code"] == "print(1)"
    assert np.allclose(entries[0]["embedding"], embedding)
    manager.close()


def test_cache_entries_are_scoped_to_the_schema(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    manager.save_cache_entry("q", ["a"], "code", np.ones(4, dtype=np.float32))

    assert manager.get_cache_entries(["a"])
    assert manager.get_cache_entries(["different"]) == []
    manager.close()


def test_database_is_usable_from_multiple_threads(tmp_path) -> None:
    """Regression: connections were leaked per call and WAL was never enabled,
    so concurrent writes from `asyncio.to_thread` produced 'database is locked'.
    """
    manager = DatabaseManager(db_path=str(tmp_path / "concurrent.db"))
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for step in range(10):
                manager.save_feedback(f"task-{index}-{step}", "print(1)")
                manager.get_feedbacks()
        except Exception as exc:  # pragma: no cover - failure is the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"concurrent access failed: {errors}"
    assert len(manager.get_feedbacks()) == 40


def test_chat_messages_round_trip_in_order(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "chat.db"))
    manager.append_chat_message("s1", "user", "first")
    manager.append_chat_message("s1", "assistant", "second")
    manager.append_chat_message("s2", "user", "other session")

    messages = manager.get_chat_messages("s1")
    assert [message["content"] for message in messages] == ["first", "second"]
    assert len(manager.get_chat_messages("s2")) == 1
    manager.close()


def test_session_deletion_removes_all_scoped_rows(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "scoped.db"))
    manager.append_chat_message("s1", "user", "hello")
    manager.save_memory(time.time(), "task", "plan", "code", "result", session_id="s1")
    manager.save_schema("t.csv", ["a"], 1, "a", session_id="s1")
    manager.save_analysis_state("s1", 1, {"objective": None})

    manager.delete_session_data("s1")

    assert manager.get_chat_messages("s1") == []
    assert manager.get_memories(session_id="s1") == []
    assert manager.get_schemas(session_id="s1") == []
    assert manager.get_analysis_state(1) is None
    manager.close()


def test_analysis_state_round_trips_and_keeps_the_latest_per_message(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "analysis_state.db"))
    manager.save_analysis_state("s1", 7, {"open_questions": ["first pass"]})
    manager.save_analysis_state("s1", 7, {"open_questions": ["revised after reflection"]})

    assert manager.get_analysis_state(7) == {"open_questions": ["revised after reflection"]}
    assert manager.get_analysis_state(999) is None
    manager.close()


def test_memory_pruning_keeps_the_most_recent(tmp_path) -> None:
    manager = DatabaseManager(db_path=str(tmp_path / "prune.db"))
    for index in range(20):
        manager.save_memory(float(index), f"task {index}", "", "", "", session_id="s1")

    manager.prune_memories(keep_last=5)
    assert len(manager.get_memories()) == 5
    manager.close()
