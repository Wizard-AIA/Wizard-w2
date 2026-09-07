"""Backups are a lifecycle-managed production capability, not dead code."""

from __future__ import annotations

import asyncio

from src.core.infra.backup_scheduler import BackupScheduler


async def test_scheduler_starts_two_tasks_and_stops_them_cleanly() -> None:
    scheduler = BackupScheduler(backup_interval_hours=24, checkpoint_interval_hours=24)

    await scheduler.start()
    assert {task.get_name() for task in scheduler._tasks} == {"db-backup", "wal-checkpoint"}

    await scheduler.stop()
    await asyncio.sleep(0)
    assert scheduler._tasks == []
