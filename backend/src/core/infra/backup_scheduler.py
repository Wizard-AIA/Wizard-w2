"""Scheduled database backup and WAL checkpoint tasks."""

from __future__ import annotations

import asyncio

from src.utils.logging import logger


class BackupScheduler:
    """Runs periodic database backups and WAL checkpoints in the background."""

    def __init__(
        self,
        backup_interval_hours: float = 24.0,
        checkpoint_interval_hours: float = 6.0,
        max_retained: int = 7,
    ):
        self._backup_interval = backup_interval_hours * 3600
        self._checkpoint_interval = checkpoint_interval_hours * 3600
        self._max_retained = max_retained
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._backup_loop(), name="db-backup"))
        self._tasks.append(asyncio.create_task(self._checkpoint_loop(), name="wal-checkpoint"))
        logger.info("backup_scheduler_started", backup_h=self._backup_interval / 3600, checkpoint_h=self._checkpoint_interval / 3600)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _backup_loop(self) -> None:
        from src.core.database import db_mgr
        while True:
            await asyncio.sleep(self._backup_interval)
            try:
                path = db_mgr.backup()
                db_mgr.prune_backups(self._max_retained)
                logger.info("scheduled_backup_completed", path=str(path))
            except Exception as exc:
                logger.error("scheduled_backup_failed", error=str(exc))

    async def _checkpoint_loop(self) -> None:
        from src.core.database import db_mgr
        while True:
            await asyncio.sleep(self._checkpoint_interval)
            try:
                db_mgr.checkpoint()
            except Exception as exc:
                logger.error("scheduled_checkpoint_failed", error=str(exc))
