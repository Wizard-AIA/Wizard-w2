"""Asking the user mid-run, without ending the run.

The existing plan gate is turn-*terminating*: the orchestrator returns
``awaiting_approval`` and the client re-sends a whole new turn carrying the
approved plan, which skips orientation. That works for the plan because the plan
is the first thing that happens and nothing has been spent yet.

It does not work for an action chosen at iteration four. Ending the turn there
would discard three iterations of investigation state and re-spend every model
call that produced it, and the loop is not deterministic enough to be sure it
would even come back to the same question. So consent for actions *within* a run
suspends instead: the turn parks on a future and resumes where it stopped.

Anything that can suspend can hang, so nothing here waits indefinitely. A
timeout, a cancelled run and a closed socket all resolve the same way -- **denied,
with a reason** -- because a paused turn the user cannot see is worse than an
action that did not happen.

Keyed by session id at module level rather than held on the ``Session``, for the
same reason ``usage_ledger`` is: this is transport-adjacent state, and routing it
through the session object would put a socket concern in the data model.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from src.core.agent.events import Emitter, EventType, emit
from src.utils.logging import logger


@dataclass(frozen=True)
class ConsentRequest:
    """One question, and enough context for the user to answer it."""

    category: str
    subject: str
    prompt: str
    detail: str = ""
    id: str = ""

    def with_id(self) -> ConsentRequest:
        return (
            self if self.id else ConsentRequest(self.category, self.subject, self.prompt, self.detail, uuid.uuid4().hex)
        )


@dataclass(frozen=True)
class ConsentDecision:
    """The answer, and why it came out that way."""

    approved: bool
    #: Empty when the user answered. Populated when nobody did, so the caller can
    #: tell "declined" from "never reached anyone" in what it reports.
    reason: str = ""


class ConsentBroker:
    """Routes a mid-run question to whoever is holding the socket."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, asyncio.Future[bool]]] = {}

    async def ask(
        self,
        session_id: str,
        request: ConsentRequest,
        emitter: Emitter | None,
        timeout: float,
    ) -> ConsentDecision:
        """Emits the request and waits for an answer, or denies with a reason."""
        request = request.with_id()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending.setdefault(session_id, {})[request.id] = future

        try:
            await emit(
                emitter,
                EventType.APPROVAL_REQUIRED,
                id=request.id,
                tool="permission",
                category=request.category,
                subject=request.subject,
                prompt=request.prompt,
                detail=request.detail,
            )
            approved = await asyncio.wait_for(future, timeout=timeout)
            return ConsentDecision(approved=approved)
        except TimeoutError:
            logger.info("Consent request timed out", category=request.category, session=session_id)
            return ConsentDecision(
                approved=False,
                reason=f"No answer within {int(timeout)}s, so it was treated as declined.",
            )
        finally:
            # Cancellation lands here too, and must leave nothing registered: a
            # future belonging to a dead run would swallow the next answer.
            self._discard(session_id, request.id)

    def resolve(self, session_id: str, request_id: str, approved: bool) -> bool:
        """Answers a waiting request. False when there was nothing waiting."""
        future = self._pending.get(session_id, {}).get(request_id)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    def abandon(self, session_id: str) -> None:
        """Denies everything outstanding, for a socket that has gone away."""
        for future in self._pending.pop(session_id, {}).values():
            if not future.done():
                future.set_result(False)

    def waiting(self, session_id: str) -> int:
        """How many requests are outstanding. Exists for tests and diagnostics."""
        return len(self._pending.get(session_id, {}))

    def reset(self) -> None:
        """Drops every outstanding request, across every session.

        For test teardown: `abandon` only reaches one session, and this is a
        process-wide singleton, so a request left behind by one test would
        otherwise still be pending when the next test's session id happens to
        collide with it. Unlike `abandon`, this does not resolve the dropped
        futures -- a test process tears down its event loop between tests, so
        setting a result on a future from a prior loop would raise instead of
        release anything.
        """
        self._pending.clear()

    def _discard(self, session_id: str, request_id: str) -> None:
        outstanding = self._pending.get(session_id)
        if outstanding is None:
            return
        outstanding.pop(request_id, None)
        if not outstanding:
            self._pending.pop(session_id, None)


consent_broker = ConsentBroker()


__all__ = ["ConsentBroker", "ConsentDecision", "ConsentRequest", "consent_broker"]
