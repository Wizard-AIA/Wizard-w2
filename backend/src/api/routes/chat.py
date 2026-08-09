"""Chat: a buffered REST endpoint and a streaming WebSocket.

Both drive the same :class:`AnalysisOrchestrator`. The WebSocket handler used to
re-implement the node sequencing by hand, which is why the cache lookup and the
fast-path router silently applied to `POST /chat` only. Here the transport does
nothing but translate events into frames.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect

from src.api.deps import SESSION_HEADER, require_api_key, require_dataset, ws_gate
from src.api.schemas import ChatRequest, ChatResponse
from src.config import settings
from src.core.agent.consent import consent_broker
from src.core.agent.events import Event, EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.session import Session, session_manager
from src.utils.errors import safe_error_message
from src.utils.logging import logger


router = APIRouter(tags=["chat"])


@router.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(
    request: ChatRequest,
    response: Response,
    session: Session = Depends(require_dataset),
) -> ChatResponse:
    """Runs a full turn and returns the finished answer.

    Use the WebSocket for token streaming; this exists for scripts and integrations.
    """
    session.append_message("user", request.message)
    collector = EventCollector()

    result = await orchestrator.run(
        session=session,
        instruction=request.message,
        mode=request.mode,
        emitter=collector,
        approved_plan=request.approved_plan,
    )

    response.headers[SESSION_HEADER] = session.id
    payload = result.to_dict()
    return ChatResponse(
        response=payload["response"],
        code=payload["code"],
        thought=payload["thought"],
        plan=payload["plan"],
        image=payload["image"],
        status=payload["status"],
        artifacts=payload["artifacts"],
        warnings=payload["warnings"],
        approval=payload["approval"],
        downloads=payload["downloads"],
        elapsed_ms=payload["elapsed_ms"],
        findings=payload["findings"],
        assumptions=payload["assumptions"],
        iterations=payload["iterations"],
        tier=payload["tier"],
        mode=payload["mode"],
        verification=payload["verification"],
        grounding=payload["grounding"],
        skills_used=payload["skills_used"],
    )


class WebSocketEmitter:
    """Serialises orchestrator events onto a socket.

    Send failures are swallowed: a client that navigated away must not surface as
    an orchestrator exception mid-run.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.closed = False

    async def __call__(self, event: Event) -> None:
        if self.closed:
            return
        try:
            await self.websocket.send_json(event.to_dict())
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True
        except Exception as exc:
            self.closed = True
            logger.debug("Dropping event, socket unusable", error=str(exc))


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket) -> None:
    """Streaming chat.

    Client frames
    -------------
    ``{"type": "message", "content": str, "mode": "auto"|"fast"|"deep"|"planning"}``
    ``{"type": "approval", "approved": bool, "id"?: str, "tool": str, "content": str, "plan"?: str, "query"?: str}``
    ``{"type": "cancel"}``  ``{"type": "ping"}``

    Server frames are the orchestrator's event types plus ``session`` and ``pong``.

    An ``approval`` frame carrying ``id`` answers a *running* turn that paused on
    a permission gate; it is routed to the consent broker and starts nothing. One
    without ``id`` is the plan gate, which ends its turn and is resumed by
    starting a new one.

    The receive loop deliberately does not await the run. It used to, which meant
    no frame sent during a turn was read until the turn finished -- so ``cancel``
    could not interrupt anything, and a mid-run consent question could never be
    answered by the only client able to answer it.
    """
    client_host = websocket.client.host if websocket.client else "unknown"
    if not ws_gate.acquire(client_host):
        await websocket.close(code=1013, reason="Too many concurrent connections.")
        return

    await websocket.accept()

    # Header only -- a query string is routinely captured in reverse-proxy and
    # load-balancer access logs, browser history and the Referer header even
    # over TLS, so a key accepted there would leak through channels the
    # WSS handshake itself never touches.
    api_key = websocket.headers.get("x-api-key")
    if settings.API_KEY and (not api_key or not hmac.compare_digest(api_key, settings.API_KEY)):
        await websocket.send_json({"type": "error", "content": "Invalid or missing API key."})
        await websocket.close(code=1008)
        ws_gate.release(client_host)
        return

    session_id = websocket.query_params.get("session") or websocket.headers.get(SESSION_HEADER.lower())
    session = session_manager.get_or_create(session_id)

    emitter = WebSocketEmitter(websocket)
    current_run: asyncio.Task | None = None
    last_code: str | None = None

    await websocket.send_json({"type": EventType.SESSION.value, "session_id": session.id})

    async def resolve_session() -> Session:
        """Re-resolves the socket's session, and counts the frame as activity.

        Binding the object once at connect meant an eviction or a TTL reap left
        the socket holding a *disposed* ``Session``. ``dispose()`` clears
        ``datasets``, so the next question answered "No dataset is loaded" for
        data the user had just uploaded, against a runtime already released.
        Sessions are capped (``SESSION_MAX_ACTIVE``, which host sizing derives
        to 7 on a 16 GB laptop) and evicted least-recently-seen, so a few tabs
        or a backend restart reach this.
        """
        nonlocal session
        live = session_manager.get(session.id)  # get() touches on a hit
        if live is not None:
            return live
        session = session_manager.create()
        # The id changed underneath the client. Without telling it, its stored
        # id keeps naming the dead session and every later REST call -- upload
        # included -- lands somewhere this socket cannot see.
        await websocket.send_json({"type": EventType.SESSION.value, "session_id": session.id})
        return session

    try:
        while True:
            payload: dict[str, Any] = await websocket.receive_json()
            kind = payload.get("type", "message")

            # Every frame, before anything branches on it. A heartbeat is proof
            # the client is still there, so it has to count against eviction:
            # `ping` used to return before the session was touched, which left
            # a connected tab holding a dataset ageing to the top of the
            # least-recently-seen order while it sat idle.
            session = await resolve_session()

            if kind == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if kind == "cancel":
                # Any turn paused on a consent question is released first, so
                # cancelling does not leave a future nobody will ever resolve.
                consent_broker.abandon(session.id)
                if current_run and not current_run.done():
                    current_run.cancel()
                await asyncio.to_thread(session.executor.interrupt)
                await websocket.send_json({"type": EventType.STATUS.value, "content": "Cancelled", "phase": "idle"})
                continue

            # A consent answer belongs to the turn already running. It is handled
            # before the busy check below, which exists to reject a *new* turn.
            if kind == "approval" and payload.get("id"):
                if not consent_broker.resolve(session.id, str(payload["id"]), bool(payload.get("approved"))):
                    logger.debug("Consent answer had nothing waiting", session=session.id)
                continue

            if current_run and not current_run.done():
                await websocket.send_json(
                    {"type": EventType.ERROR.value, "content": "A run is already in progress on this session."}
                )
                continue

            # An empty frame is a no-op in every case, so it is discarded before
            # any state check: a blank message should not raise "no dataset".
            instruction = (payload.get("content") or "").strip()
            if not instruction:
                continue

            if not session.has_data:
                await websocket.send_json(
                    {
                        "type": EventType.ERROR.value,
                        "content": "No dataset is loaded. Upload a file before asking a question.",
                    }
                )
                continue

            mode = payload.get("mode", "auto")
            approved_plan: str | None = None
            approved_search: str | None = None

            if kind == "approval":
                if not payload.get("approved"):
                    await websocket.send_json(
                        {"type": EventType.STATUS.value, "content": "Plan rejected", "phase": "idle"}
                    )
                    continue
                tool = payload.get("tool")
                if tool == "web_search":
                    approved_search = payload.get("query") or ""
                else:
                    approved_plan = payload.get("plan") or instruction
                    # Already approved, so the gate must not fire again — but the
                    # investigation still gets its full budget. Downgrading to
                    # `fast` here would have made approving a plan silently
                    # reduce the work done to carry it out.
                    mode = "auto" if mode == "planning" else mode
            else:
                session.append_message("user", instruction)

            async def run_turn(
                run_session: Session,
                run_instruction: str,
                run_mode: str,
                run_approved_plan: str | None,
                run_approved_search: str | None,
            ):
                """Runs one turn and reports its own outcome.

                Takes every value it needs as a plain parameter rather than
                closing over the receive loop's locals. The loop reassigns
                ``session``/``instruction``/``mode`` on its very next
                iteration, and a coroutine created by ``ensure_future`` does
                not start running until the loop yields -- so a free variable
                here would risk reading next turn's values instead of this
                one's. Passing them as arguments at the call site below fixes
                what value each parameter holds independent of when the
                coroutine actually starts.

                The error handling lives in here rather than around an
                ``await`` on the task, because the receive loop must stay free
                to deliver the frames a paused turn is waiting for.
                """
                nonlocal last_code
                try:
                    result = await orchestrator.run(
                        session=run_session,
                        instruction=run_instruction,
                        mode=run_mode,
                        emitter=emitter,
                        approved_plan=run_approved_plan,
                        approved_search=run_approved_search,
                        previous_code=last_code,
                        # This socket can carry a consent question to a human and
                        # bring the answer back, so gated actions may pause here
                        # instead of resolving to a denial.
                        can_prompt=True,
                    )
                    if result.code:
                        last_code = result.code
                except asyncio.CancelledError:
                    await emitter(
                        Event(
                            type=EventType.STATUS,
                            data={"content": "Run cancelled", "phase": "idle"},
                        )
                    )
                    raise
                except Exception as exc:
                    message = safe_error_message(exc, "Chat run failed", session=run_session.id)
                    await emitter(Event(type=EventType.ERROR, data={"content": message}))
                finally:
                    consent_broker.abandon(run_session.id)

            current_run = asyncio.ensure_future(
                run_turn(session, instruction, mode, approved_plan, approved_search)
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", session=session.id)
    except Exception as exc:
        message = safe_error_message(exc, "WebSocket handler crashed")
        try:
            await websocket.send_json({"type": EventType.ERROR.value, "content": message})
        except Exception as send_exc:
            logger.debug("Could not deliver the error frame; the socket is already gone", error=str(send_exc))
    finally:
        # Release before cancelling: a turn parked on a consent question would
        # otherwise sit until the timeout expired before noticing it was dead.
        consent_broker.abandon(session.id)
        if current_run and not current_run.done():
            current_run.cancel()
        emitter.closed = True
        ws_gate.release(client_host)
