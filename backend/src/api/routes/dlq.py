"""Dead-letter queue inspection and replay endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import require_api_key
from src.core.database import db_mgr
from src.core.infra.queue import get_queue

router = APIRouter(prefix="/api/dlq", tags=["dlq"], dependencies=[Depends(require_api_key)])


@router.get("")
async def list_dead_letters(limit: int = 50):
    """List unresolved dead-letter entries."""
    return db_mgr.get_dead_letters(limit=limit)


@router.post("/{job_id}/replay")
async def replay_dead_letter(job_id: str):
    """Mark a dead-letter entry as replayed."""
    entries = db_mgr.get_dead_letters(limit=1000)
    match = next((e for e in entries if e["id"] == job_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Dead-letter entry not found")
    db_mgr.mark_dlq_replayed(job_id)
    return {"status": "replayed", "job_id": job_id}


@router.delete("/{job_id}")
async def delete_dead_letter(job_id: str):
    """Permanently remove a dead-letter entry."""
    db_mgr.delete_dead_letter(job_id)
    return {"status": "deleted", "job_id": job_id}
