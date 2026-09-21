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
