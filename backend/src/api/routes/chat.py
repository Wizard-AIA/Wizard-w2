"""Chat: a buffered REST endpoint and a streaming WebSocket.

Both drive the same :class:`AnalysisOrchestrator`. The WebSocket handler used to
re-implement the node sequencing by hand, which is why the cache lookup and the
fast-path router silently applied to `POST /chat` only. Here the transport does
nothing but translate events into frames.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from src.api.deps import (
    SESSION_HEADER,
    get_consent_broker,
    get_orchestrator,
    require_api_key,
    resolve_chat_session,
    ws_client_ip,
    ws_client_key,
    ws_gate,
    ws_ip_gate,
)
from src.api.schemas import ChatRequest, ChatResponse
from src.config import settings
from src.core.agent.consent import ConsentBroker
from src.core.agent.events import Event, EventCollector, EventType
from src.core.agent.orchestrator import AnalysisOrchestrator
from src.core.agent.routing import is_interrupt_intent
from src.core.infra.idempotency import IdempotencyConflict, get_idempotency_store, request_fingerprint
from src.core.session import Session, session_manager
from src.utils.errors import safe_error_message
from src.utils.logging import logger


router = APIRouter(tags=["chat"])

_session_locks: dict[str, asyncio.Lock] = {}

_TERMINAL_TYPES = frozenset({EventType.FINAL, EventType.ERROR, EventType.CANCELLED})


def _is_terminal(event: Event) -> bool:
    if event.type in _TERMINAL_TYPES:
        return True
    return event.type is EventType.APPROVAL_REQUIRED and not event.data.get("id")


class TerminalEmitter:
    """Track one turn's terminal event and suppress duplicate terminals."""

    def __init__(self, inner):
        self.inner = inner
        self.terminal_sent = False

    async def __call__(self, event: Event) -> None:
        if _is_terminal(event):
            if self.terminal_sent:
                return
            self.terminal_sent = True
            if event.type is EventType.ERROR:
                event.data.setdefault("code", "internal")
        result = self.inner(event)
        if asyncio.iscoroutine(result):
            await result


def _release_subagent_runtimes(session: Session) -> None:
    for child_id in list(getattr(session, "_subagent_ids", ())):
        try:
            session.dispose_subagent(child_id)
        except Exception as exc:  # cleanup must not hide the cancellation
            logger.debug("Could not release a subagent runtime", child_id=child_id, error=str(exc))


async def _cancel_task(
    session: Session,
    task: asyncio.Task | None,
    emitter: TerminalEmitter,
    reason: str,
    consent_broker: ConsentBroker | None = None,
) -> bool:
    """Cancel, interrupt and clean up a turn, waiting at most five seconds."""
    if task is None or task.done():
        return False
    if consent_broker is not None:
        consent_broker.abandon(session.id)
    task.cancel()
    try:
        await asyncio.to_thread(session.executor.interrupt)
    except Exception as exc:
        logger.debug("Executor interrupt failed during cancellation", session=session.id, error=str(exc))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except (asyncio.CancelledError, TimeoutError):
        pass
    finally:
        session.reset_task()
        session.end_turn()
        _release_subagent_runtimes(session)
    if not emitter.terminal_sent:
        await emitter(Event(type=EventType.CANCELLED, data={"reason": reason}))
    return True


@router.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(
    request: ChatRequest,
    response: Response,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    session: Session = Depends(resolve_chat_session),
    orchestrator: AnalysisOrchestrator = Depends(get_orchestrator),
) -> ChatResponse:
    """Runs a full turn and returns the finished answer.

    Use the WebSocket for token streaming; this exists for scripts and integrations.
    """
    store = get_idempotency_store()
    fingerprint = request_fingerprint(
        session_id=session.id,
        message=request.message,
        mode=request.mode,
        approved_plan=request.approved_plan,
    )

    if x_idempotency_key:
        try:
            cached = store.get_cached(x_idempotency_key, fingerprint)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if cached:
            response.headers[SESSION_HEADER] = session.id
            return cached

        idemp_lock = store.acquire_lock(x_idempotency_key)
        if idemp_lock.locked():
            raise HTTPException(status_code=409, detail="Request already in progress")
        await idemp_lock.acquire()

    if session.id not in _session_locks:
        _session_locks[session.id] = asyncio.Lock()
    session_lock = _session_locks[session.id]

    if session_lock.locked():
        if x_idempotency_key:
            store.release(x_idempotency_key)
        raise HTTPException(status_code=409, detail="Analysis already in progress for this session")

    await session_lock.acquire()

    try:
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
        chat_response = ChatResponse(
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
            route=payload.get("route", {}),
        )

        if x_idempotency_key:
            store.store_result(x_idempotency_key, fingerprint, chat_response)

        return chat_response
    finally:
        session_lock.release()
        if x_idempotency_key:
            store.release(x_idempotency_key)


@router.post("/api/chat/stream")
async def chat_stream(
    body: ChatRequest,
    session: Session = Depends(resolve_chat_session),
    orchestrator: AnalysisOrchestrator = Depends(get_orchestrator),
):
    """Server-Sent Events alternative to WebSocket for proxy-hostile environments."""

    session_lock = _session_locks.setdefault(session.id, asyncio.Lock())
    if session_lock.locked():
        raise HTTPException(status_code=409, detail="Analysis already in progress for this session")
    await session_lock.acquire()
    session.append_message("user", body.message)
    session.begin_turn(body.mode)

    async def event_generator():
        collector = EventCollector()
        tracked = TerminalEmitter(collector)
        await tracked(Event(type=EventType.STATUS, data={"content": "Understanding your request", "phase": "routing"}))
        # Run orchestrator in background task
        task = asyncio.create_task(
            orchestrator.run(
                session=session,
                instruction=body.message,
                mode=body.mode,
                emitter=tracked,
                approved_plan=body.approved_plan,
            )
        )
        try:
            # Stream events as they arrive.
            seen = 0
            while not task.done():
                await asyncio.sleep(0.05)
                events = collector.events[seen:]
                seen += len(events)
                for evt in events:
                    yield f"data: {json.dumps(evt.to_dict())}\n\n"
            events = collector.events[seen:]
            for evt in events:
                yield f"data: {json.dumps(evt.to_dict())}\n\n"
            result = task.result()
            session.end_turn(
                code=getattr(result, "code", None) or None,
                awaiting_plan=(getattr(result, "pending_approval", None) or {}).get("plan")
                if getattr(result, "status", "") == "awaiting_approval"
                else None,
            )
            if not tracked.terminal_sent:
                await tracked(
                    Event(
                        type=EventType.ERROR,
                        data={"content": "Turn ended without a terminal frame", "code": "internal"},
                    )
                )
            yield f"data: {json.dumps({'type': 'result', 'content': result.to_dict() if hasattr(result, 'to_dict') else str(result)})}\n\n"
        except asyncio.CancelledError:
            await _cancel_task(session, task, tracked, "disconnect")
            raise
        except Exception as exc:
            err_msg = safe_error_message(exc, "Streaming request failed", session=session.id)
            session.end_turn()
            if not tracked.terminal_sent:
                await tracked(Event(type=EventType.ERROR, data={"content": err_msg, "code": "internal"}))
            yield f"data: {json.dumps({'type': 'error', 'content': err_msg, 'code': 'internal'})}\n\n"
        finally:
            session_lock.release()

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class WebSocketEmitter:
    """Streams orchestrator events to the client over a WebSocket with backpressure control."""

    _HIGH_WATERMARK = 256
    _DROPPABLE = frozenset({"status", "progress"})  # non-critical frame types

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._HIGH_WATERMARK)
        self._sender_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background sender coroutine."""
        self._sender_task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        """Signal the sender to flush and stop."""
        self.closed = True
        if self._sender_task:
            await self._queue.put(None)  # sentinel
            await self._sender_task

    async def _drain(self) -> None:
        """Background loop: pull frames from the queue and send them."""
        while True:
            frame = await self._queue.get()
            if frame is None:
                break
            try:
                await self.websocket.send_json(frame.to_dict())
            except (WebSocketDisconnect, RuntimeError):
                self.closed = True
                break
            except Exception as exc:
                self.closed = True
                logger.debug("Dropping event, socket unusable", error=str(exc))
                break

    async def __call__(self, event: Event) -> None:
        if self.closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop non-critical frames under backpressure
            event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
            if event_type in self._DROPPABLE:
                return  # silently drop
            # For critical frames, make room by discarding oldest droppable
            # or block briefly
            try:
                await asyncio.wait_for(self._queue.put(event), timeout=1.0)
            except TimeoutError:
                pass  # drop if still full after 1s


@router.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
    orchestrator: AnalysisOrchestrator = Depends(get_orchestrator),
    consent_broker: ConsentBroker = Depends(get_consent_broker),
) -> None:
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
    # Validate Origin header to prevent cross-site WebSocket hijacking
    origin = websocket.headers.get("origin", "")
    if origin and settings.CORS_ALLOW_ORIGINS:
        allowed_origins = [o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",")]
        if origin not in allowed_origins:
            await websocket.close(code=4003, reason="Origin not allowed")
            return

    # Resolved once here, before accept(): the composite (IP, session) key
    # gates fairness among sessions sharing an address (NAT, reverse proxy);
    # the raw IP additionally gates the address itself, since a session id is
    # client-supplied and a fresh one per connection would otherwise mint a
    # fresh concurrency bucket per connection. Both keys are reused to
    # release their gates once the socket closes.
    client_host = ws_client_key(websocket)
    client_addr = ws_client_ip(websocket)
    if not ws_gate.acquire(client_host):
        await websocket.close(code=1013, reason="Too many concurrent connections.")
        return
    if not ws_ip_gate.acquire(client_addr):
        ws_gate.release(client_host)
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
        ws_ip_gate.release(client_addr)
        return

    session_id = websocket.query_params.get("session") or websocket.headers.get(SESSION_HEADER.lower())
    resolved = session_manager.get(session_id) if session_id is not None else None
    session = resolved if resolved is not None else session_manager.create()

    emitter = WebSocketEmitter(websocket)
    await emitter.start()
    current_run: asyncio.Task | None = None
    current_emitter: TerminalEmitter | None = None
    cancel_reason: str | None = None

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
                cancelled = await _cancel_task(
                    session, current_run, current_emitter or TerminalEmitter(emitter), "user", consent_broker
                )
                if cancelled:
                    current_run = None
                if not cancelled:
                    await websocket.send_json({"type": EventType.STATUS.value, "content": "Cancelled", "phase": "idle"})
                continue

            # A consent answer belongs to the turn already running. It is handled
            # before the busy check below, which exists to reject a *new* turn.
            if kind == "approval" and payload.get("id"):
                if not consent_broker.resolve(session.id, str(payload["id"]), bool(payload.get("approved"))):
                    logger.debug("Consent answer had nothing waiting", session=session.id)
                continue

            if current_run and not current_run.done():
                if is_interrupt_intent(instruction := (payload.get("content") or "")):
                    await _cancel_task(
                        session, current_run, current_emitter or TerminalEmitter(emitter), "user", consent_broker
                    )
                    current_run = None
                else:
                    await websocket.send_json(
                        {
                            "type": EventType.ERROR.value,
                            "content": "A run is already in progress on this session.",
                            "code": "busy",
                        }
                    )
                continue

            # An empty frame is a no-op in every case, so it is discarded before
            # any state check: a blank message should not raise "no dataset".
            instruction = (payload.get("content") or "").strip()
            if not instruction:
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

            session.begin_turn(mode)
            turn_emitter = TerminalEmitter(emitter)
            current_emitter = turn_emitter
            cancel_reason = None
            await turn_emitter(
                Event(type=EventType.STATUS, data={"content": "Understanding your request", "phase": "routing"})
            )

            async def run_turn(
                run_session: Session,
                run_instruction: str,
                run_mode: str,
                run_approved_plan: str | None,
                run_approved_search: str | None,
                run_emitter: TerminalEmitter,
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
                nonlocal cancel_reason
                result = None
                try:
                    result = await orchestrator.run(
                        session=run_session,
                        instruction=run_instruction,
                        mode=run_mode,
                        emitter=run_emitter,
                        approved_plan=run_approved_plan,
                        approved_search=run_approved_search,
                        previous_code=run_session.task.last_code,
                        # This socket can carry a consent question to a human and
                        # bring the answer back, so gated actions may pause here
                        # instead of resolving to a denial.
                        can_prompt=True,
                    )
                    if not run_emitter.terminal_sent:
                        await run_emitter(
                            Event(
                                type=EventType.ERROR,
                                data={"content": "Turn ended without a terminal frame", "code": "internal"},
                            )
                        )
                except asyncio.CancelledError:
                    session.reset_task()
                    session.end_turn()
                    _release_subagent_runtimes(session)
                    if not run_emitter.terminal_sent:
                        await run_emitter(
                            Event(
                                type=EventType.CANCELLED,
                                data={"reason": cancel_reason or "user"},
                            )
                        )
                    raise
                except Exception as exc:
                    message = safe_error_message(exc, "Chat run failed", session=run_session.id)
                    session.end_turn()
                    await run_emitter(Event(type=EventType.ERROR, data={"content": message, "code": "internal"}))
                finally:
                    consent_broker.abandon(run_session.id)
                    if result is not None:
                        session.end_turn(
                            code=result.code or None,
                            awaiting_plan=(result.pending_approval or {}).get("plan")
                            if result.status == "awaiting_approval"
                            else None,
                        )

            current_run = asyncio.ensure_future(
                run_turn(session, instruction, mode, approved_plan, approved_search, turn_emitter)
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", session=session.id)
        cancel_reason = "disconnect"
        await _cancel_task(
            session, current_run, current_emitter or TerminalEmitter(emitter), "disconnect", consent_broker
        )
        current_run = None
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
            cancel_reason = "disconnect"
            await _cancel_task(
                session, current_run, current_emitter or TerminalEmitter(emitter), "disconnect", consent_broker
            )
            current_run = None
        await emitter.stop()
        ws_gate.release(client_host)
        ws_ip_gate.release(client_addr)
