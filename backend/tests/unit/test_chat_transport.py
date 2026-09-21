from __future__ import annotations

import asyncio

import pytest

from src.api.routes.chat import TerminalEmitter, _cancel_task
from src.core.agent.events import Event, EventCollector, EventType


@pytest.mark.asyncio
async def test_terminal_guard_emits_exactly_one_terminal_frame() -> None:
    collector = EventCollector()
    emitter = TerminalEmitter(collector)
    await emitter(Event(type=EventType.FINAL, data={"response": "ok"}))
    await emitter(Event(type=EventType.ERROR, data={"content": "late"}))

    assert [event.type for event in collector.events] == [EventType.FINAL]
    assert emitter.terminal_sent is True


@pytest.mark.asyncio
async def test_cancel_task_interrupts_and_emits_cancelled(session) -> None:
    collector = EventCollector()
    emitter = TerminalEmitter(collector)
    interrupted = False

    def interrupt() -> None:
        nonlocal interrupted
        interrupted = True

    session.executor.interrupt = interrupt
    task = asyncio.create_task(asyncio.sleep(60))
    await _cancel_task(session, task, emitter, "user")

    assert interrupted is True
    assert [event.type for event in collector.events] == [EventType.CANCELLED]
    assert collector.events[0].data["reason"] == "user"
    assert session.task.status == "idle"


@pytest.mark.asyncio
async def test_cancelling_keeps_the_chart_of_the_turn_before(session) -> None:
    """A cancelled turn drops what it was doing, not the chart the user is still looking at."""
    session.task.last_code = "plt.plot([1])"
    session.begin_turn()
    task = asyncio.create_task(asyncio.sleep(60))
    await _cancel_task(session, task, TerminalEmitter(EventCollector()), "user")

    assert session.task.last_code == "plt.plot([1])"
    assert session.task.status == "idle"


class _FinishingOrchestrator:
    async def run(self, *, session, instruction, mode, emitter, approved_plan=None, **_):
        await emitter(Event(type=EventType.FINAL, data={"response": "ok"}))

        class _Result:
            def to_dict(self) -> dict:
                return {"response": "ok"}

        return _Result()


@pytest.mark.asyncio
async def test_sse_lock_is_not_held_by_a_stream_that_was_never_read(session) -> None:
    """The client can leave before the first read. The generator never runs, so a
    lock taken in the endpoint would stay held and every later message would 409."""
    from src.api.routes.chat import _session_locks, chat_stream
    from src.api.schemas import ChatRequest

    response = await chat_stream(ChatRequest(message="hello"), session, _FinishingOrchestrator())
    lock = _session_locks[session.id]
    assert not lock.locked()

    await response.body_iterator.aclose()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_sse_lock_is_released_when_the_stream_finishes(session) -> None:
    from src.api.routes.chat import _session_locks, chat_stream
    from src.api.schemas import ChatRequest

    response = await chat_stream(ChatRequest(message="hello"), session, _FinishingOrchestrator())
    frames = [frame async for frame in response.body_iterator]

    assert any('"final"' in frame for frame in frames)
    assert frames[-1] == "data: [DONE]\n\n"
    assert not _session_locks[session.id].locked()


@pytest.mark.asyncio
async def test_sse_second_message_while_one_runs_is_refused(session) -> None:
    from fastapi import HTTPException

    from src.api.routes.chat import _session_locks, chat_stream
    from src.api.schemas import ChatRequest

    lock = _session_locks.setdefault(session.id, asyncio.Lock())
    await lock.acquire()
    try:
        with pytest.raises(HTTPException) as refused:
            await chat_stream(ChatRequest(message="hello"), session, _FinishingOrchestrator())
        assert refused.value.status_code == 409
    finally:
        lock.release()
