"""Session lifecycle."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from src.api.deps import SESSION_HEADER, get_session, require_api_key
from src.api.schemas import ReportResponse, SessionResponse
from src.core.reporting import reporting_engine
from src.core.session import Session, session_manager


router = APIRouter(prefix="/api", tags=["session"])


@router.post("/session", response_model=SessionResponse, dependencies=[Depends(require_api_key)])
async def create_session(response: Response) -> SessionResponse:
    session = session_manager.create()
    response.headers[SESSION_HEADER] = session.id
    return SessionResponse(**session.describe())


@router.get("/session", response_model=SessionResponse)
async def describe_session(response: Response, session: Session = Depends(get_session)) -> SessionResponse:
    """Returns the caller's session, creating one only if no id was sent."""
    response.headers[SESSION_HEADER] = session.id
    return SessionResponse(**session.describe())


@router.delete("/session", dependencies=[Depends(require_api_key)])
async def delete_session(session: Session = Depends(get_session)) -> dict:
    """Drops the session, its container and its persisted rows."""
    session_manager.drop(session.id)
    return {"message": "Session cleared.", "session_id": session.id}


@router.post("/session/reset", response_model=SessionResponse, dependencies=[Depends(require_api_key)])
async def reset_namespace(response: Response, session: Session = Depends(get_session)) -> SessionResponse:
    """Clears sandbox variables while keeping the loaded dataset."""
    session.executor.reset()
    response.headers[SESSION_HEADER] = session.id
    return SessionResponse(**session.describe())


@router.get("/report", response_model=ReportResponse)
async def generate_report(hours: int = 24, session: Session = Depends(get_session)) -> ReportResponse:
    """Executive summary of this session's analyses."""
    payload = reporting_engine.summary_payload(
        timespan_seconds=max(1, hours) * 3600,
        session_id=session.id,
    )
    return ReportResponse(**payload)
