"""Behavioural matrix for turn routing.

These assert *which workflow a message gets*, never prose. They are the
regression suite for the v1.0.14 goal: conversation stays conversation, simple
questions stay cheap, and complex work keeps its full machinery.
"""

from __future__ import annotations

import pytest

from src.core.agent.routing import (
    Complexity,
    Intent,
    PlanPolicy,
    TurnContext,
    Workflow,
    apply_mode,
    escalate,
    extract_signals,
    is_interrupt_intent,
    route_turn,
)
from src.core.llm.router import TaskTier


COLUMNS = ("revenue", "region", "salary", "churn", "month", "order_id", "name")

WITH_DATA = TurnContext(has_dataset=True, columns=COLUMNS)
AFTER_ANALYSIS = TurnContext(has_dataset=True, columns=COLUMNS, has_prior_turn=True)
NO_DATA = TurnContext()


def wf(message: str, ctx: TurnContext = WITH_DATA, mode: str = "auto") -> Workflow:
    return route_turn(message, ctx, mode).workflow


# --------------------------------------------------------------------------- #
# Conversation never enters an analytic workflow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    ["hi", "hello", "thanks", "okay", "good morning", "Thanks!", "ok cool", "hey there", "bye", "cheers", "lol nice"],
)
@pytest.mark.parametrize("ctx", [WITH_DATA, AFTER_ANALYSIS, NO_DATA], ids=["data", "after-analysis", "no-data"])
@pytest.mark.parametrize("mode", ["auto", "fast", "deep", "planning"])
def test_social_turns_are_conversation_in_every_mode(message: str, ctx: TurnContext, mode: str) -> None:
    route = route_turn(message, ctx, mode)
    assert route.workflow is Workflow.CONVERSE
    assert route.plan is PlanPolicy.NONE
    assert not route.verify
    assert not route.escalate, "a decided social turn must not carry an escalation hatch"
    assert not route.needs_data
    assert route.task_tier is TaskTier.LIGHTWEIGHT


def test_hi_after_an_analysis_does_not_continue_the_analysis() -> None:
    first = route_turn("analyze revenue by region", AFTER_ANALYSIS)
    second = route_turn("hi", AFTER_ANALYSIS)
    assert first.workflow is Workflow.AGENTIC
    assert second.workflow is Workflow.CONVERSE


@pytest.mark.parametrize("message", ["which region is highest", "this is fine", "think about it", "history of sales"])
def test_marker_words_do_not_fire_inside_other_words(message: str) -> None:
    # `hi` in "which"/"this"/"think"/"history": the substring bug.
    assert not extract_signals(message, WITH_DATA).social or message == "this is fine"


def test_short_analytic_request_is_not_chitchat() -> None:
    # "plot revenue" is 12 characters; the old length rule called it chitchat.
    route = route_turn("plot revenue", WITH_DATA)
    assert route.workflow is Workflow.DIRECT
    assert route.intent is Intent.VISUALIZATION


@pytest.mark.parametrize("message", ["plot this", "chart it", "make a histogram"])
def test_bare_plot_requests_are_visualisations(message: str) -> None:
    assert route_turn(message, AFTER_ANALYSIS).intent is Intent.VISUALIZATION


def test_unlisted_greetings_are_still_conversation() -> None:
    # Not on any list: no task evidence, so conversation, and the reply decides.
    route = route_turn("hola, buenas tardes", WITH_DATA)
    assert route.workflow is Workflow.CONVERSE
    assert route.escalate, "an unknown language must be able to hand the turn to the loop"


def test_what_does_that_mean_is_not_the_statistic() -> None:
    assert wf("what does that mean?", AFTER_ANALYSIS) is Workflow.CONVERSE
    assert extract_signals("what is the mean?", WITH_DATA).operations == {"aggregate"}


def test_column_named_like_a_word_does_not_hijack_questions_about_wizard() -> None:
    route = route_turn("what is your name?", AFTER_ANALYSIS)
    assert route.workflow is Workflow.CONVERSE
    assert route.intent is Intent.FOLLOW_UP


# --------------------------------------------------------------------------- #
# Follow-ups
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "why did you choose that chart?",
        "explain the previous result",
        "what did you just do?",
        "why?",
        "how did you get that",
    ],
)
def test_questions_about_the_previous_answer_are_conversation_with_context(message: str) -> None:
    route = route_turn(message, AFTER_ANALYSIS)
    assert route.workflow is Workflow.CONVERSE
    assert route.intent is Intent.FOLLOW_UP
    assert route.escalate, "a follow-up may still need new data; the reply decides"


def test_a_short_question_with_no_prior_turn_is_not_a_follow_up() -> None:
    assert route_turn("why?", WITH_DATA).intent is not Intent.FOLLOW_UP


def test_context_availability_is_not_continuation_intent() -> None:
    # Same prior turn, three different messages, three different workflows.
    assert wf("north america is highest", AFTER_ANALYSIS) is not Workflow.AGENTIC
    assert wf("thanks", AFTER_ANALYSIS) is Workflow.CONVERSE
    assert wf("now analyze churn instead", AFTER_ANALYSIS) is Workflow.AGENTIC


# --------------------------------------------------------------------------- #
# Direct questions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "show me the columns",
        "what columns are in this dataset?",
        "how many rows are there?",
        "what are the column names",
        "show the first 5 rows",
        "what are the data types",
        "describe the schema",
        "any missing values?",
    ],
)
def test_structure_questions_inspect_without_planning(message: str) -> None:
    route = route_turn(message, WITH_DATA)
    assert route.workflow is Workflow.INSPECT, message
    assert route.plan is PlanPolicy.NONE
    assert not route.verify
    assert route.task_tier is TaskTier.LIGHTWEIGHT


@pytest.mark.parametrize(
    "message",
    [
        "show me the rows where revenue > 100",
        "how many rows have revenue over 500",
        "first 10 rows where region is east",
    ],
)
def test_a_filter_is_a_computation_not_a_frame_fact(message: str) -> None:
    assert wf(message) is not Workflow.INSPECT, message


@pytest.mark.parametrize(
    "message",
    [
        "give me the average salary",
        "what is the mean?",
        "find the average revenue",
        "calculate the total revenue",
        "count the orders",
        "show me revenue by region",
        "which region is strongest?",
        "median salary",
    ],
)
def test_simple_computation_skips_the_planner_and_verification(message: str) -> None:
    route = route_turn(message, WITH_DATA)
    assert route.workflow is Workflow.DIRECT, message
    assert route.plan is PlanPolicy.NONE
    assert not route.verify
    assert route.complexity is Complexity.SIMPLE
    assert route.budget_mode("auto") == "fast"


# --------------------------------------------------------------------------- #
# Agentic work keeps its machinery
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "analyze why customer churn increased and determine the most likely causes, then validate them statistically",
        "identify the strongest drivers of churn and validate whether they are statistically significant",
        "analyze this dataset",
    ],
)
def test_complex_analysis_is_planned_and_verified(message: str) -> None:
    route = route_turn(message, WITH_DATA)
    assert route.workflow is Workflow.AGENTIC, message
    assert route.plan is PlanPolicy.SELF
    assert route.verify
    assert route.complexity is Complexity.COMPLEX


def test_multi_step_task_gets_the_deep_budget() -> None:
    route = route_turn(
        "clean the dataset, identify anomalies, analyze churn drivers, validate statistically, and generate a report",
        WITH_DATA,
    )
    assert route.intent is Intent.MULTI_STEP
    assert route.plan is PlanPolicy.SELF
    assert route.deep
    assert route.budget_mode("auto") == "deep"


@pytest.mark.parametrize("message", ["find why revenue dropped", "analyze revenue", "compare revenue across regions"])
def test_specific_investigations_loop_without_a_separate_plan(message: str) -> None:
    route = route_turn(message, WITH_DATA)
    assert route.workflow is Workflow.AGENTIC, message
    assert route.plan is PlanPolicy.NONE
    assert route.verify


@pytest.mark.parametrize(
    "message",
    [
        "create a plan first and don't execute anything",
        "create a detailed analysis plan first",
        "create a plan for predicting churn",
        "give me a plan, but do not run anything",
    ],
)
def test_explicit_plan_requests_plan_and_stop(message: str) -> None:
    route = route_turn(message, WITH_DATA)
    assert route.workflow is Workflow.PLAN_ONLY, message
    assert route.plan is PlanPolicy.ONLY


@pytest.mark.parametrize(
    "message", ["build a model to plan inventory", "create a column for plan type", "premium plan churn"]
)
def test_the_word_plan_alone_is_not_a_plan_request(message: str) -> None:
    assert wf(message) is not Workflow.PLAN_ONLY, message


def test_a_typed_confirmation_executes_the_waiting_plan() -> None:
    ctx = TurnContext(has_dataset=True, columns=COLUMNS, has_prior_turn=True, pending_plan=True)
    assert route_turn("execute the plan", ctx).workflow is Workflow.EXECUTE_PLAN
    assert route_turn("yes", ctx).workflow is Workflow.EXECUTE_PLAN
    # Without a waiting plan the same words are just conversation.
    assert route_turn("yes", AFTER_ANALYSIS).workflow is Workflow.CONVERSE
    # A new task while a plan waits is a new task, not a confirmation.
    assert route_turn("plot revenue by month", ctx).workflow is Workflow.DIRECT


# --------------------------------------------------------------------------- #
# Modes are policy over intent
# --------------------------------------------------------------------------- #
def test_fast_removes_planning_and_verification_but_still_computes() -> None:
    route = route_turn("identify the strongest drivers of churn and validate them statistically", WITH_DATA, "fast")
    assert route.workflow is Workflow.AGENTIC
    assert route.plan is PlanPolicy.NONE
    assert not route.verify and not route.deep
    assert route.budget_mode("fast") == "fast"


def test_deep_investigates_even_a_simple_question_but_not_a_greeting() -> None:
    deep = route_turn("give me the average salary", WITH_DATA, "deep")
    assert deep.workflow is Workflow.AGENTIC and deep.verify and deep.deep
    assert wf("hi", WITH_DATA, "deep") is Workflow.CONVERSE
    assert wf("how many rows are there?", WITH_DATA, "deep") is Workflow.INSPECT


def test_legacy_planning_mode_gates_analytic_work_only() -> None:
    gated = route_turn("give me the average salary", WITH_DATA, "planning")
    assert gated.plan is PlanPolicy.GATED
    assert wf("hello", WITH_DATA, "planning") is Workflow.CONVERSE
    assert wf("show me the columns", WITH_DATA, "planning") is Workflow.INSPECT


def test_an_explicit_plan_request_wins_over_fast_mode() -> None:
    assert wf("create a plan first and don't run anything", WITH_DATA, "fast") is Workflow.PLAN_ONLY


def test_mode_never_changes_what_a_message_is() -> None:
    for message in ["hi", "thanks", "what columns are there?", "plot revenue by month"]:
        intents = {route_turn(message, AFTER_ANALYSIS, mode).intent for mode in ("auto", "fast", "deep", "planning")}
        assert len(intents) == 1, (message, intents)


# --------------------------------------------------------------------------- #
# Missing data is a conversation, not an error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message", ["calculate the average salary", "analyze this dataset", "show me the columns", "plot revenue"]
)
def test_data_workflows_without_a_dataset_become_a_conversation_about_what_is_missing(message: str) -> None:
    route = route_turn(message, NO_DATA)
    assert route.workflow is Workflow.CONVERSE
    assert route.needs_data


def test_escalation_without_a_dataset_stays_conversational() -> None:
    route = route_turn("and last year?", NO_DATA)
    assert route.workflow is Workflow.CONVERSE
    again = escalate(route, has_dataset=False)
    assert again.workflow is Workflow.CONVERSE and again.needs_data


def test_escalation_with_a_dataset_runs_the_loop_under_the_users_mode() -> None:
    first = route_turn("and last year?", AFTER_ANALYSIS)
    assert first.escalate
    loop = escalate(first, has_dataset=True, mode="auto")
    assert loop.workflow is Workflow.AGENTIC and loop.source == "escalation"
    assert escalate(first, has_dataset=True, mode="fast").verify is False


# --------------------------------------------------------------------------- #
# Model tier follows the route, so the small model is never sent real work
# --------------------------------------------------------------------------- #
def test_tier_follows_the_route() -> None:
    assert route_turn("hi", WITH_DATA).task_tier is TaskTier.LIGHTWEIGHT
    assert route_turn("and last year?", AFTER_ANALYSIS).task_tier is TaskTier.STANDARD
    assert route_turn("average salary", WITH_DATA).task_tier is TaskTier.STANDARD
    assert route_turn("analyze this dataset", WITH_DATA).task_tier is TaskTier.REASONING_HEAVY


def test_apply_mode_is_idempotent() -> None:
    route = route_turn("analyze this dataset", WITH_DATA)
    for mode in ("auto", "fast", "deep", "planning"):
        once = apply_mode(route, mode)
        assert apply_mode(once, mode).to_dict() == once.to_dict()


def test_route_serialises_without_the_message_text() -> None:
    secret = "revenue for customer ACME-SECRET-123"
    payload = str(route_turn(secret, WITH_DATA).to_dict()) + str(extract_signals(secret, WITH_DATA).to_dict())
    assert "ACME-SECRET-123" not in payload


# --------------------------------------------------------------------------- #
# Interrupt intent (only consulted while a turn is running)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "stop",
        "Stop!",
        "cancel",
        "please stop",
        "stop it",
        "stop that now",
        "abort",
        "never mind",
        "nevermind",
        "forget it",
        "enough",
    ],
)
def test_a_message_that_only_says_stop_is_an_interrupt(message: str) -> None:
    assert is_interrupt_intent(message)


@pytest.mark.parametrize(
    "message",
    [
        "stop losses by region",
        "why did the campaign stop",
        "cancel rate by month",
        "how many cancelled orders",
        "stopwatch",
        "hi",
        "",
    ],
)
def test_data_that_contains_the_word_is_not_an_interrupt(message: str) -> None:
    assert not is_interrupt_intent(message)


#: Built inside the test, not in the parametrisation: a 200,000 character test id
#: is not something every platform's test runner and cache can store.
HOSTILE = {
    "spaces-after-enough": lambda: "enough" + " " * 200_000 + "x",
    "repeated-stop-words": lambda: "stop" + " it" * 50_000 + "!" + " " * 50_000 + "x",
    "repeated-and": lambda: "why " + "and " * 60_000,
    "spaces-around-then": lambda: "then" + " " * 200_000 + "and then" + " " * 200_000 + "x",
    "spaces-after-comma": lambda: "a," + " " * 200_000 + "b",
    "repeated-why": lambda: "why " * 60_000 + "you?",
    "repeated-article": lambda: "create " + "a " * 60_000 + "column",
    "newlines-and-spaces": lambda: ("x" + " " * 5_000 + "\n") * 100,
}


@pytest.mark.parametrize("case", list(HOSTILE))
def test_hostile_input_is_routed_in_bounded_time(case: str) -> None:
    """A chat message is user-controlled. Long runs of spaces or repeated words must not stall the router."""
    import time

    hostile = HOSTILE[case]()
    started = time.perf_counter()
    is_interrupt_intent(hostile)
    route_turn(hostile, AFTER_ANALYSIS)
    assert time.perf_counter() - started < 0.5


def test_line_breaks_still_separate_clauses() -> None:
    assert extract_signals("average salary\nplot churn").n_clauses == 2


def test_whitespace_does_not_change_a_route() -> None:
    assert wf("average   salary\n\nby   region") == wf("average salary by region")
    assert is_interrupt_intent("  stop   it  ") and is_interrupt_intent("Never  mind!")


def test_a_schema_question_is_one_shot_in_every_mode() -> None:
    for mode in ("auto", "fast", "deep", "planning"):
        assert route_turn("how many rows are there?", WITH_DATA, mode).budget_mode(mode) == "fast", mode


# --------------------------------------------------------------------------- #
# Found by the adversarial review: statements that name a column, and instructions
# that say "do not run it" without saying "plan"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "I keep hearing the word churn",
        "my region is Europe",
        "salary sounds good",
        "I am worried about churn",
        "what does churn mean?",
        "the revenue is what it is",
    ],
)
def test_naming_a_column_is_not_asking_for_an_analysis(message: str) -> None:
    """The reply decides: it can hand the turn to the loop, but chat never runs one unasked."""
    route = route_turn(message, AFTER_ANALYSIS)
    assert route.workflow is Workflow.CONVERSE and route.escalate, route.reasons


@pytest.mark.parametrize(
    "message",
    [
        "show me salary by region",
        "list the regions",
        "which region is strongest?",
        "which region has the highest revenue",
        "what is the highest salary",
        "who has the lowest salary",
    ],
)
def test_asking_for_something_about_a_column_still_computes(message: str) -> None:
    assert wf(message, AFTER_ANALYSIS) is Workflow.DIRECT


@pytest.mark.parametrize(
    "message",
    [
        "outline the analysis but do not run it",
        "describe what you would do to find why churn is high",
        "how would you approach the churn analysis",
        "what would you do about revenue by region",
        "analyse churn by region without running anything yet",
        "sketch your approach to the revenue drop, don't execute it",
    ],
)
def test_an_instruction_not_to_run_is_a_plan_request_in_every_mode(message: str) -> None:
    for mode in ("auto", "fast", "deep"):
        route = route_turn(message, AFTER_ANALYSIS, mode)
        assert route.workflow is Workflow.PLAN_ONLY, (message, mode, route.reasons)


@pytest.mark.parametrize(
    "message",
    ["don't start with the null rows, average the salary", "run a regression of churn on salary"],
)
def test_words_about_the_data_are_not_a_do_not_run_instruction(message: str) -> None:
    assert wf(message, AFTER_ANALYSIS) is not Workflow.PLAN_ONLY


@pytest.mark.parametrize("message", ["create a column for plan type", "add a column that doubles revenue"])
def test_making_a_column_is_a_transformation_not_a_question_about_structure(message: str) -> None:
    assert wf(message) is Workflow.DIRECT
