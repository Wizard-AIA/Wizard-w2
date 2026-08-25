"""Event protocol between the orchestrator and any transport.

The orchestrator does not know about WebSockets. It emits typed events; the
transport decides how to serialise them. That is what lets the same run drive a
streaming socket, a buffered REST response and a test collector without the
duplicated node-sequencing loop the previous WebSocket handler carried.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    SESSION = "session"
    STATUS = "status"
    STEP_START = "step_start"
    STEP_END = "step_end"
    REASONING_DELTA = "reasoning_delta"
    PLAN_DELTA = "plan_delta"
    CONTENT_DELTA = "content_delta"
    CODE = "code"
    STDOUT = "stdout"
    ARTIFACT = "artifact"
    APPROVAL_REQUIRED = "approval_required"
    WARNING = "warning"
    ERROR = "error"
    FINAL = "final"

    # ------------------------------------------------------------------ #
    # Investigation frames.
    #
    # The run is a loop, not a pipeline, so "which step of five are we on" no
    # longer describes it. These carry what the agent chose to do next and what
    # it learned, which is the part a user actually needs in order to trust a
    # multi-step answer. The frames above are all still emitted, so a client
    # that ignores these degrades to the previous experience rather than
    # breaking.
    # ------------------------------------------------------------------ #
    ITERATION_START = "iteration_start"  # {n, budget, mode}
    ACTION = "action"  # {kind, goal, rationale}
    OBSERVATION = "observation"  # {summary, ok, truncated, chars}
    FINDING = "finding"  # {text}
    PLAN_REVISED = "plan_revised"  # {plan, why}
    ASSUMPTION = "assumption"  # {text, kind}
    VERIFICATION = "verification"  # {status, detail}
    CRITIC_FINDING = "critic_finding"  # {category, severity, message, suggested_reaction}
    ROUTE_COMPARISON = (
        "route_comparison"  # {group, verdict, routes, agreement_detail, more_appropriate, why, residual_uncertainty}
    )
    CONFIDENCE = "confidence"  # {verdict, components, reasons, stop_reason, stop_detail}
    #: Which skill informed this turn. Emitted rather than left implicit in a
    #: prompt nobody sees: "the agent can name which skill informed a decision"
    #: is a Milestone 5 acceptance criterion, and a frame is the only way it can
    #: be true on screen rather than by inference.
    SKILL = "skill"  # {name, layer, score, phase}
    #: An analysis has recurred enough times to be worth naming. Carries the
    #: offer only -- nothing is written until the user confirms.
    SKILL_CANDIDATE = "skill_candidate"  # {id, kind, label, instruction, occurrences, suggested_name}
    #: What the turn cost. Absent under local-only, where there is no meter.
    USAGE = "usage"  # {calls, total_tokens, cost_usd, any_cloud, estimated}

    # ------------------------------------------------------------------ #
    # Subagent frames (Milestone 7).
    #
    # A branch's own iteration_start/action/observation/step_start/step_end/
    # status/assumption/code/stdout frames are the *existing* types, just
    # additively tagged with `branch` by `BranchEmitter` -- reusing them would
    # be wrong for the top-level `action`/`observation` pair (whose "closes the
    # most recent open entry" matching only holds under strict seriality), but
    # is exactly right for everything else, since each branch's own sequence is
    # still strictly serial even though branches run concurrently with each
    # other. These two frames exist only to bound a branch's lifetime for a UI
    # that wants to group its tagged frames into a panel.
    # ------------------------------------------------------------------ #
    SUBAGENT_START = "subagent_start"  # {branch, goal, group}
    SUBAGENT_END = "subagent_end"  # {branch, group, ok, cost_usd, total_tokens, calls}


class Phase(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    SEARCHING = "searching"
    DECIDING = "deciding"
    INSPECTING = "inspecting"
    CONSULTING = "consulting"
    GENERATING = "generating"
    EXECUTING = "executing"
    CORRECTING = "correcting"
    REFLECTING = "reflecting"
    INVESTIGATING_PARALLEL = "investigating_parallel"
    REVIEWING = "reviewing"
    VERIFYING = "verifying"
    ANSWERING = "answering"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "at": self.at, **self.data}


# An emitter may be sync or async; ``emit`` normalises both.
Emitter = Callable[[Event], Awaitable[None] | None]


async def emit(emitter: Emitter | None, event_type: EventType, **data: Any) -> None:
    """Sends one event, tolerating a missing or synchronous emitter."""
    if emitter is None:
        return
    import asyncio

    result = emitter(Event(type=event_type, data=data))
    if asyncio.iscoroutine(result):
        _ = await result


class BranchEmitter:
    """Wraps an emitter, tagging every event with which subagent branch sent it.

    A subagent runs the *same* handlers (``_act_code``/``_generate``/
    ``_execute``) the main loop does, unmodified, so it emits the same event
    types. What distinguishes its frames from the main thread's -- and from a
    concurrent sibling branch's -- is purely this additive ``branch`` key.
    ``setdefault`` rather than assignment so a frame that already names its own
    branch (there isn't one today, but nesting is not precluded) is not
    overwritten by an outer wrapper.
    """

    def __init__(self, inner: Emitter | None, branch: str):
        self._inner = inner
        self._branch = branch

    def __call__(self, event: Event) -> Awaitable[None] | None:
        event.data.setdefault("branch", self._branch)
        if self._inner is None:
            return None
        return self._inner(event)


class EventCollector:
    """Buffers events. Used by the REST path and by tests."""

    def __init__(self):
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [event for event in self.events if event.type is event_type]

    def text_of(self, event_type: EventType) -> str:
        return "".join(str(event.data.get("content", "")) for event in self.of_type(event_type))

    def as_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]
