"""Message class x mode: what each combination runs, and what it costs.

Each cell drives a real turn with a model that answers every call the same way,
so the numbers are the workflow's own: how many model calls, whether a planner
ran, whether verification ran. The table is the product's promise in one place:

- A greeting or a question about the data costs one call in every mode.
- One figure or chart never plans and never verifies unless the user asked for depth.
- Fast removes machinery. Deep adds it. Neither changes what a message *is*.
- A complex question keeps the full workflow in Auto.

A change that makes `hi` expensive, or a one-line question slow, fails here first.
"""

from __future__ import annotations

import pytest
from stubs import ScriptedLLM

from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.session import Session
from src.utils.logging import logger


# One reply that is acceptable to every role: a decision (`ACTION: answer`), a plan,
# a program that prints, a verification program, and a written answer.
UNIVERSAL = "ACTION: answer\nGOAL: report it\n1. Compute the total of A\n```python\nprint('VERIFIED: 15')\n```\nThe total of A is 15."

CHAT = "hi"
SCHEMA = "how many rows are there?"
ONE_FIGURE = "calculate the total of column A"
CHART = "plot column A"
COMPLEX = "why is column A so different across column B, and which values of B drive it? then build a model and summarise the findings"
ASK_FOR_PLAN = "make a plan for analysing column A across column B"

MODES = ["auto", "fast", "deep", "planning"]

#: (message, mode) -> (workflow, planner ran, verification ran, model calls)
#: Counts are exact: they are what a turn costs, and a change to them is a change to the product.
MATRIX = {
    # Conversation and questions about the data: one call, whatever the depth.
    (CHAT, "auto"): ("converse", False, False, 1),
    (CHAT, "fast"): ("converse", False, False, 1),
    (CHAT, "deep"): ("converse", False, False, 1),
    (CHAT, "planning"): ("converse", False, False, 1),
    (SCHEMA, "auto"): ("inspect", False, False, 1),
    (SCHEMA, "fast"): ("inspect", False, False, 1),
    (SCHEMA, "deep"): ("inspect", False, False, 1),
    (SCHEMA, "planning"): ("inspect", False, False, 1),
    # One figure or chart: code, then the answer. Deep adds an investigation and
    # verification; plan mode stops at the plan for the user to confirm.
    (ONE_FIGURE, "auto"): ("direct", False, False, 2),
    (ONE_FIGURE, "fast"): ("direct", False, False, 2),
    (ONE_FIGURE, "deep"): ("agentic", False, True, 4),
    (ONE_FIGURE, "planning"): ("agentic", True, False, 1),
    (CHART, "auto"): ("direct", False, False, 2),
    (CHART, "fast"): ("direct", False, False, 2),
    (CHART, "deep"): ("agentic", False, True, 4),
    (CHART, "planning"): ("agentic", True, False, 1),
    # A complex question keeps the full workflow. Fast strips the planner and verification.
    (COMPLEX, "auto"): ("agentic", True, True, 5),
    (COMPLEX, "fast"): ("agentic", False, False, 2),
    (COMPLEX, "deep"): ("agentic", True, True, 5),
    (COMPLEX, "planning"): ("agentic", True, False, 1),
    # An explicit request for a plan is an instruction: every mode plans and stops.
    (ASK_FOR_PLAN, "auto"): ("plan_only", True, False, 1),
    (ASK_FOR_PLAN, "fast"): ("plan_only", True, False, 1),
    (ASK_FOR_PLAN, "deep"): ("plan_only", True, False, 1),
    (ASK_FOR_PLAN, "planning"): ("plan_only", True, False, 1),
}


@pytest.fixture
def traces(monkeypatch) -> list[dict]:
    seen: list[dict] = []
    real_info = logger.info

    def capture(message, *args, **kwargs):
        if message == "Turn trace":
            seen.append(kwargs)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(logger, "info", capture)
    return seen


@pytest.mark.usefixtures("real_routing")
@pytest.mark.parametrize(("message", "mode"), list(MATRIX))
async def test_each_message_class_costs_what_the_matrix_says(
    loaded_session: Session, monkeypatch, traces, message: str, mode: str
) -> None:
    workflow, planned, verified, calls = MATRIX[(message, mode)]
    stub = ScriptedLLM([UNIVERSAL] * 12)
    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", stub)
    collector = EventCollector()
    loaded_session.append_message("user", message)

    result = await orchestrator.run(session=loaded_session, instruction=message, mode=mode, emitter=collector)

    assert result.route["workflow"] == workflow, result.route
    trace = traces[-1]
    assert (trace["planner_used"], bool(collector.of_type(EventType.VERIFICATION))) == (planned, verified), trace
    assert trace["llm_call_count"] == calls, trace
    assert trace["llm_call_count"] == len(stub.prompts)


@pytest.mark.usefixtures("real_routing")
@pytest.mark.parametrize("mode", MODES)
async def test_a_greeting_never_costs_more_than_a_schema_question_or_a_figure(
    loaded_session: Session, monkeypatch, traces, mode: str
) -> None:
    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", ScriptedLLM([UNIVERSAL] * 12))
    calls: dict[str, int] = {}
    for label, message in (("chat", CHAT), ("schema", SCHEMA), ("figure", ONE_FIGURE)):
        loaded_session.append_message("user", message)
        await orchestrator.run(session=loaded_session, instruction=message, mode=mode, emitter=EventCollector())
        calls[label] = traces[-1]["llm_call_count"]

    assert calls["chat"] <= calls["schema"] <= calls["figure"], calls
