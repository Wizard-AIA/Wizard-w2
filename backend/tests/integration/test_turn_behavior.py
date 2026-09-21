"""Behavioural evaluation of whole turns, with real routing.

Each test drives `AnalysisOrchestrator.run` with a scripted model and asserts what
*ran*: was a planner called, was code generated, how many model calls did the
turn cost, which frames were emitted, what state did it leave. Never prose.

This is the regression suite for the v1.0.14 goal: `hi` after an analysis is a
conversation, a schema question costs one call, one figure needs no planner, a
complex question keeps the full loop, and no finished task defines the next message.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from stubs import ScriptedLLM

from src.config import settings
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.semantic_cache import semantic_cache
from src.core.session import Session
from src.utils.logging import logger as orchestrator_logger


CODE = "```python\nprint(df['A'].sum())\n```"
PLAN = "1. Compute the total of A\n2. Print it"

#: Frames that only an analysis turn may emit.
ANALYSIS_FRAMES = {
    EventType.CODE,
    EventType.STDOUT,
    EventType.ITERATION_START,
    EventType.ACTION,
    EventType.PLAN_DELTA,
    EventType.VERIFICATION,
}


@pytest.fixture
def llm(monkeypatch):
    def install(responses: list[str]) -> ScriptedLLM:
        stub = ScriptedLLM(responses)
        monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", stub)
        return stub

    return install


def types_of(collector: EventCollector) -> set[EventType]:
    return {event.type for event in collector.events}


def step_labels(collector: EventCollector) -> list[str]:
    return [event.data.get("label", "") for event in collector.of_type(EventType.STEP_START)]


async def turn(session: Session, message: str, mode: str = "auto", **kwargs):
    collector = EventCollector()
    session.append_message("user", message)
    result = await orchestrator.run(session=session, instruction=message, mode=mode, emitter=collector, **kwargs)
    return result, collector


# --------------------------------------------------------------------------- #
# Conversation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", ["hi", "thanks", "okay", "good morning", "hello there"])
@pytest.mark.parametrize("mode", ["auto", "fast", "deep", "planning"])
async def test_a_greeting_costs_one_call_and_runs_nothing_else(loaded_session: Session, llm, message, mode) -> None:
    stub = llm(["Hello! What would you like to look at?"])
    result, collector = await turn(loaded_session, message, mode)

    assert result.status == "completed"
    assert result.route["workflow"] == "converse"
    assert len(stub.prompts) == 1, "one reply, no planner, no code, no verification, no review"
    assert not types_of(collector) & ANALYSIS_FRAMES
    assert not step_labels(collector)
    assert result.code == "" and result.iterations == 0
    assert "Hello" in result.answer


async def test_hi_after_an_analysis_does_not_continue_it(loaded_session: Session, llm) -> None:
    stub = llm([CODE, "The total of A is 15.", "Hi again."])
    first, _ = await turn(loaded_session, "calculate the total of column A")
    assert first.route["workflow"] == "direct" and "print" in first.code
    cached_before = semantic_cache.lookup("hi", ["A", "B", "C"], scope=orchestrator._cache_scope(loaded_session))
    calls_after_first = len(stub.prompts)

    second, collector = await turn(loaded_session, "hi")

    assert second.route["workflow"] == "converse"
    assert len(stub.prompts) == calls_after_first + 1
    assert not types_of(collector) & ANALYSIS_FRAMES
    assert cached_before is None
    assert semantic_cache.lookup("hi", ["A", "B", "C"], scope=orchestrator._cache_scope(loaded_session)) is None
    # The chart code the user might still revise survives a chat turn.
    assert loaded_session.task.last_code and "sum" in loaded_session.task.last_code


async def test_a_conversational_reply_is_persisted_but_never_learned_from(loaded_session: Session, llm) -> None:
    llm(["Hi!"])
    result, _ = await turn(loaded_session, "hi")

    row = loaded_session.history()[-1]
    assert row["role"] == "assistant" and row["content"] == "Hi!"
    assert result.message_id
    assert loaded_session.last_task_digest() is None, "a greeting is not a task for a follow-up to refer to"


async def test_the_model_being_down_does_not_turn_a_greeting_into_an_error(
    loaded_session: Session, monkeypatch
) -> None:
    from src.core.llm.provider import LLMUnavailableError

    class Dead:
        async def stream_to(self, *args, **kwargs):
            raise LLMUnavailableError("nothing is listening")

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", Dead())
    result, collector = await turn(loaded_session, "hi")

    assert result.status == "completed"
    assert result.answer
    assert not collector.of_type(EventType.ERROR)


async def test_chatting_before_uploading_anything_works(session: Session, llm) -> None:
    llm(["Hello! Upload a file whenever you are ready."])
    result, collector = await turn(session, "hi")
    assert result.status == "completed" and result.route["workflow"] == "converse"
    assert not collector.of_type(EventType.ERROR)


# --------------------------------------------------------------------------- #
# Direct questions
# --------------------------------------------------------------------------- #
async def test_a_schema_question_costs_one_call_and_writes_no_code(loaded_session: Session, llm) -> None:
    stub = llm(["The dataset has 5 rows and 3 columns: A, B and C."])
    result, collector = await turn(loaded_session, "how many rows are there?")

    assert result.route["workflow"] == "inspect"
    assert len(stub.prompts) == 1, "the answer only: the frame supplied the facts"
    assert EventType.CODE not in types_of(collector)
    assert not any("plan" in label.lower() for label in step_labels(collector))
    assert "5" in stub.prompts[0], "the answer is written from the real frame facts"
    assert result.status == "completed"


async def test_one_figure_needs_no_planner_and_no_verification(loaded_session: Session, llm) -> None:
    stub = llm([CODE, "The total of A is 15."])
    result, collector = await turn(loaded_session, "calculate the total of column A")

    assert result.route["workflow"] == "direct" and result.route["plan"] == "none"
    assert len(stub.prompts) == 2, "code, then the answer"
    assert EventType.PLAN_DELTA not in types_of(collector)
    assert EventType.VERIFICATION not in types_of(collector)
    assert "review" not in step_labels(collector) and "Reviewing results" not in step_labels(collector)
    assert "15" in result.answer


async def test_a_chart_request_is_direct(loaded_session: Session, llm) -> None:
    stub = llm([CODE, "Here is the chart."])
    result, _ = await turn(loaded_session, "plot column A")
    assert result.route["intent"] == "visualization" and result.route["workflow"] == "direct"
    assert len(stub.prompts) == 2


# --------------------------------------------------------------------------- #
# Complex work keeps its machinery
# --------------------------------------------------------------------------- #
async def test_a_complex_question_is_planned_then_investigated_and_verified(loaded_session: Session, llm) -> None:
    stub = llm([PLAN, CODE, "answer", "VERIFIED: matches", "The total of A is 15."])
    result, collector = await turn(
        loaded_session,
        "identify the strongest drivers of column A and validate whether they are statistically significant",
    )

    assert result.route["workflow"] == "agentic" and result.route["plan"] == "self"
    assert EventType.PLAN_DELTA in types_of(collector) or "Planning the analysis" in step_labels(collector)
    assert len(stub.prompts) >= 4, "plan, code and at least one more: investigation and verification"


async def test_fast_mode_removes_the_planner_even_for_a_complex_question(loaded_session: Session, llm) -> None:
    stub = llm([CODE, "Done."])
    result, collector = await turn(
        loaded_session,
        "identify the strongest drivers of column A and validate whether they are statistically significant",
        mode="fast",
    )
    assert result.route["plan"] == "none" and result.route["verify"] is False
    assert len(stub.prompts) == 2
    assert "Planning the analysis" not in step_labels(collector)


# --------------------------------------------------------------------------- #
# Plans: explicit, gated, and confirmed by typing
# --------------------------------------------------------------------------- #
async def test_an_explicit_plan_request_plans_and_stops(loaded_session: Session, llm) -> None:
    stub = llm([PLAN])
    result, collector = await turn(loaded_session, "create a plan first and don't execute anything")

    assert result.status == "awaiting_approval"
    assert result.pending_approval["tool"] == "execute_plan"
    assert len(stub.prompts) == 1 and EventType.CODE not in types_of(collector)
    assert loaded_session.task.pending_plan == result.plan
    assert loaded_session.task.status == "awaiting_plan"


async def test_typing_execute_the_plan_runs_that_plan_and_not_those_words(loaded_session: Session, llm) -> None:
    stub = llm([PLAN, CODE, "The total is 15."])
    planned, _ = await turn(loaded_session, "create a plan to calculate the total of column A first")
    assert planned.status == "awaiting_approval"
    calls_after_plan = len(stub.prompts)

    done, collector = await turn(loaded_session, "execute the plan", mode="fast")

    assert done.status == "completed" and "print" in done.code
    assert not any("Planning" in label for label in step_labels(collector)), "the plan is not re-made"
    assert len(stub.prompts) == calls_after_plan + 2, "code and the answer"
    assert loaded_session.task.pending_plan is None
    assert "calculate the total of column A" in done.analysis["objective"]["question"], "the original request ran"


async def test_a_new_task_supersedes_a_waiting_plan(loaded_session: Session, llm) -> None:
    llm([PLAN, CODE, "The total is 15."])
    await turn(loaded_session, "create a plan to calculate the total of column A first")
    assert loaded_session.task.pending_plan

    result, _ = await turn(loaded_session, "calculate the total of column A")
    assert result.route["workflow"] == "direct"
    assert loaded_session.task.pending_plan is None


async def test_plan_mode_gates_analysis_but_never_a_greeting(loaded_session: Session, llm) -> None:
    llm([PLAN, "Hi!"])
    gated, _ = await turn(loaded_session, "calculate the total of column A", mode="planning")
    assert gated.status == "awaiting_approval" and gated.route["plan"] == "gated"

    hello, _ = await turn(loaded_session, "hi", mode="planning")
    assert hello.status == "completed" and hello.route["workflow"] == "converse"


async def test_deployment_wide_approval_gates_analysis_but_not_conversation_or_schema(
    loaded_session: Session, llm, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "AGENT_REQUIRE_APPROVAL", True)
    llm([PLAN, "Hi!", "5 rows."])

    analysis, _ = await turn(loaded_session, "calculate the total of column A")
    assert analysis.status == "awaiting_approval", "the deployment's approval policy still holds for analysis"

    hello, _ = await turn(loaded_session, "hi")
    assert hello.status == "completed"
    schema, _ = await turn(loaded_session, "how many rows are there?")
    assert schema.status == "completed"


async def test_a_cached_solution_does_not_skip_a_plan_the_user_must_approve(loaded_session: Session, llm) -> None:
    llm([CODE, "The total is 15.", PLAN])
    await turn(loaded_session, "calculate the total of column A")
    assert semantic_cache.lookup(
        "calculate the total of column A",
        ["A", "B", "C"],
        scope=orchestrator._cache_scope(loaded_session, _direct_route()),
    )

    gated, _ = await turn(loaded_session, "calculate the total of column A", mode="planning")
    assert gated.status == "awaiting_approval"


def _direct_route():
    from src.core.agent.routing import Complexity, Intent, Route, Workflow

    return Route(Intent.COMPUTATION, Workflow.DIRECT, Complexity.SIMPLE)


# --------------------------------------------------------------------------- #
# Escalation: the reply is the classifier when routing is unsure
# --------------------------------------------------------------------------- #
async def test_a_follow_up_that_needs_the_data_escalates_and_the_sentinel_never_shows(
    loaded_session: Session, llm
) -> None:
    stub = llm([CODE, "The total is 15.", "[[NEEDS_ANALYSIS]]", CODE, "It is 15 again."])
    await turn(loaded_session, "calculate the total of column A")

    result, collector = await turn(loaded_session, "and again?")

    routes = [event.data for event in collector.of_type(EventType.ROUTE)]
    assert [r["workflow"] for r in routes] == ["converse", "agentic"]
    assert routes[1]["source"] == "escalation"
    shown = "".join(event.data.get("content", "") for event in collector.of_type(EventType.CONTENT_DELTA))
    assert "NEEDS_ANALYSIS" not in shown
    assert result.status == "completed" and "print" in result.code
    assert len(stub.prompts) >= 4


async def test_a_follow_up_about_the_previous_answer_is_answered_from_it(loaded_session: Session, llm) -> None:
    stub = llm([CODE, "The total of A is 15.", "I summed column A because you asked for its total."])
    await turn(loaded_session, "calculate the total of column A")

    result, collector = await turn(loaded_session, "why did you do that?")

    assert result.route["workflow"] == "converse" and result.route["intent"] == "follow_up"
    conversation_prompt = stub.prompts[-1]
    assert "previous_analysis" in conversation_prompt and "sum" in conversation_prompt
    assert EventType.CODE not in {e.type for e in collector.events[-8:]}


async def test_escalation_with_no_dataset_stays_a_conversation(session: Session, llm) -> None:
    llm(["I need a file to look at first. Upload a CSV and ask again."])
    result, _ = await turn(session, "and what about last year?")
    assert result.status == "completed" and result.route["workflow"] == "converse"


# --------------------------------------------------------------------------- #
# Route frame contract
# --------------------------------------------------------------------------- #
async def test_the_route_frame_comes_before_any_work_and_carries_the_mode(loaded_session: Session, llm) -> None:
    llm([CODE, "The total is 15."])
    _, collector = await turn(loaded_session, "calculate the total of column A", mode="fast")

    kinds = [event.type for event in collector.events]
    first_status = collector.events[0]
    assert first_status.type is EventType.STATUS and first_status.data["phase"] == "routing"
    route_at = kinds.index(EventType.ROUTE)
    assert route_at < kinds.index(EventType.CODE)
    assert collector.events[route_at].data["mode"] == "fast"
    assert kinds.count(EventType.FINAL) == 1


async def test_final_carries_the_route_and_status(loaded_session: Session, llm) -> None:
    llm(["Hi!"])
    _, collector = await turn(loaded_session, "hi")
    final = collector.of_type(EventType.FINAL)[0].data
    assert final["route"]["workflow"] == "converse" and final["status"] == "completed"


# --------------------------------------------------------------------------- #
# What a finished (or interrupted) task leaves behind
# --------------------------------------------------------------------------- #
async def test_a_cancelled_turn_leaves_no_task_state(loaded_session: Session, monkeypatch) -> None:
    started = asyncio.Event()

    class Slow:
        async def acomplete(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(30)

        async def stream_to(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(30)

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", Slow())
    loaded_session.task.pending_plan = "an older plan"
    task = asyncio.ensure_future(
        orchestrator.run(
            session=loaded_session, instruction="calculate the total of column A", emitter=EventCollector()
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loaded_session.task.status == "idle"
    assert loaded_session.task.pending_plan is None, "a cancelled turn must not leave a plan waiting"


async def test_a_dataset_swap_between_turns_drops_the_chart_code(loaded_session: Session, llm) -> None:
    llm([CODE, "Chart done."])
    await turn(loaded_session, "plot column A")
    assert loaded_session.task.last_code

    loaded_session.add_dataset("other.csv", pd.DataFrame({"Z": [1, 2, 3]}))
    assert loaded_session.task.last_code is None


async def test_failed_turn_does_not_leave_the_session_busy(loaded_session: Session, monkeypatch) -> None:
    from src.core.llm.provider import LLMUnavailableError

    class Dead:
        async def acomplete(self, *args, **kwargs):
            raise LLMUnavailableError("down")

        async def stream_to(self, *args, **kwargs):
            raise LLMUnavailableError("down")

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", Dead())
    result, _ = await turn(loaded_session, "identify the drivers of column A and validate them statistically")
    assert result.status in {"failed", "completed"}
    assert loaded_session.task.status == "idle"


# --------------------------------------------------------------------------- #
# Mode is policy, never intent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["auto", "fast", "deep", "planning"])
async def test_every_mode_answers_a_schema_question_the_same_way(loaded_session: Session, llm, mode) -> None:
    stub = llm(["5 rows."])
    result, _ = await turn(loaded_session, "how many rows are there?", mode)
    assert result.route["workflow"] == "inspect" and len(stub.prompts) == 1


async def test_deep_mode_investigates_a_simple_question_thoroughly(loaded_session: Session, llm) -> None:
    stub = llm(["", CODE, "x", "VERIFIED: ok", "The total is 15."])
    result, _ = await turn(loaded_session, "calculate the total of column A", mode="deep")
    assert result.route["workflow"] == "agentic" and result.route["verify"] is True
    assert len(stub.prompts) > 2


# --------------------------------------------------------------------------- #
# Observability: one text-free trace per turn, from the same code that calls the model
# --------------------------------------------------------------------------- #
@pytest.fixture
def traces(monkeypatch) -> list[dict]:
    seen: list[dict] = []
    real_info = orchestrator_logger.info

    def capture(message, *args, **kwargs):
        if message == "Turn trace":
            seen.append(kwargs)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(orchestrator_logger, "info", capture)
    return seen


async def test_a_greeting_writes_a_trace_with_one_call_and_the_converse_budget(
    loaded_session: Session, llm, traces
) -> None:
    llm(["Hi!"])
    await turn(loaded_session, "hi")

    assert len(traces) == 1
    trace = traces[0]
    assert trace["workflow"] == "converse" and trace["llm_call_count"] == 1
    assert trace["generation_budgets"] == {"converse": settings.output_budget("converse")}
    assert trace["planner_used"] is False and trace["termination_reason"] == "completed"


async def test_the_trace_counts_every_model_call_the_turn_made(loaded_session: Session, llm, traces) -> None:
    stub = llm([CODE, "The total of A is 15."])
    await turn(loaded_session, "calculate the total of column A")

    assert len(traces) == 1
    trace = traces[0]
    assert trace["workflow"] == "direct"
    assert trace["llm_call_count"] == len(stub.prompts)
    assert trace["generation_budgets"]["answer"] <= 1536, "a direct answer does not get the agentic allowance"
    assert trace["context_chars"] == sum(len(prompt) for prompt in stub.prompts)


async def test_a_trace_never_contains_the_message_or_the_prompt(loaded_session: Session, llm, traces) -> None:
    llm(["Hi!"])
    await turn(loaded_session, "hello there, my secret is swordfish")

    assert "swordfish" not in repr(traces)


async def test_a_cancelled_turn_still_writes_its_trace(loaded_session: Session, monkeypatch, traces) -> None:
    class Hanging:
        async def acomplete(self, *_a, **_k):
            await asyncio.sleep(60)

        async def stream_to(self, *_a, **_k):
            await asyncio.sleep(60)

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", Hanging())
    task = asyncio.create_task(turn(loaded_session, "hi"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [trace["termination_reason"] for trace in traces] == ["cancelled"]
