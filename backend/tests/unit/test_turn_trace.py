from __future__ import annotations

from src.core.agent.routing import Complexity, Intent, PlanPolicy, Route, Workflow
from src.core.agent.trace import TurnTrace


def _route(workflow: Workflow = Workflow.DIRECT, plan: PlanPolicy = PlanPolicy.NONE) -> Route:
    return Route(Intent.COMPUTATION, workflow, Complexity.SIMPLE, plan=plan)


def test_the_record_is_flat_and_carries_no_message_text() -> None:
    trace = TurnTrace(turn_id="turn-1")
    trace.observe_route(_route())
    trace.record_call("code", 4096, 1200, "local-model")
    record = trace.to_log()

    assert record["turn_id"] == "turn-1"
    assert record["workflow"] == "direct"
    assert record["generation_budgets"] == {"code": 4096}
    assert "message" not in record and "prompt" not in record


def test_calls_are_counted_and_sized_from_one_place() -> None:
    trace = TurnTrace(turn_id="t")
    trace.record_call("code", 4096, 1000, "m1")
    trace.record_call("code", 6144, 500, "m1")
    trace.record_call("answer", 1536, 300, "m2")

    assert trace.llm_call_count == 3
    assert trace.context_chars == 1800
    assert trace.generation_budgets == {"code": 6144, "answer": 1536}
    assert trace.models_used == ["m1", "m2"]
    assert trace.planner_used is False


def test_a_plan_call_marks_the_planner_as_used() -> None:
    trace = TurnTrace(turn_id="t")
    trace.record_call("plan", 2048, 800, "m")
    assert trace.planner_used is True


def test_routing_again_overwrites_the_route_it_describes() -> None:
    """A conversational reply that escalates is routed twice; the record shows the last route."""
    trace = TurnTrace(turn_id="t")
    trace.observe_route(_route(Workflow.CONVERSE))
    trace.observe_route(_route(Workflow.AGENTIC, PlanPolicy.SELF))
    assert trace.workflow == "agentic"
    assert trace.plan_policy == "self"
