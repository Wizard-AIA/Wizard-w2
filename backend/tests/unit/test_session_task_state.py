"""Task state is transient and separate from conversation.

A finished task must not define the next message. These pin what a task leaves
behind, when it is dropped, and that the conversation and the dataset survive.
"""

from __future__ import annotations

import pandas as pd

from src.core.session import Session


def test_a_new_session_has_no_task_and_no_prior_turn(session: Session) -> None:
    ctx = session.turn_context()
    assert session.task.status == "idle"
    assert ctx["has_prior_turn"] is False
    assert ctx["pending_plan"] is False
    assert ctx["has_dataset"] is False


def test_turn_context_carries_the_dataset_columns(loaded_session: Session) -> None:
    ctx = loaded_session.turn_context()
    assert ctx["has_dataset"] is True
    assert set(ctx["columns"]) == {"A", "B", "C"}


def test_a_turn_runs_and_leaves_nothing_transient_behind(loaded_session: Session) -> None:
    number = loaded_session.begin_turn("agentic")
    assert number == 1 and loaded_session.task.status == "running"

    loaded_session.end_turn(code="print(df.A.sum())")
    assert loaded_session.task.status == "idle"
    assert loaded_session.task.workflow == ""
    assert loaded_session.task.last_code == "print(df.A.sum())"


def test_a_conversational_turn_does_not_erase_the_chart_the_user_is_looking_at(loaded_session: Session) -> None:
    loaded_session.begin_turn("direct")
    loaded_session.end_turn(code="plt.plot(df.A)")
    loaded_session.begin_turn("converse")
    loaded_session.end_turn()
    assert loaded_session.task.last_code == "plt.plot(df.A)"


def test_a_plan_waits_for_a_decision_and_a_new_turn_supersedes_it(loaded_session: Session) -> None:
    loaded_session.begin_turn("plan_only")
    loaded_session.end_turn(awaiting_plan="1. clean 2. model")
    assert loaded_session.task.status == "awaiting_plan"
    assert loaded_session.turn_context()["pending_plan"] is True

    # Any later turn that does not carry the plan clears it.
    loaded_session.begin_turn("converse")
    loaded_session.end_turn()
    assert loaded_session.task.pending_plan is None
    assert loaded_session.turn_context()["pending_plan"] is False


def test_changing_the_dataset_drops_the_plan_and_the_chart_code(loaded_session: Session) -> None:
    loaded_session.begin_turn("plan_only")
    loaded_session.end_turn(code="plt.plot(df.A)", awaiting_plan="a plan about the old data")

    loaded_session.add_dataset("other.csv", pd.DataFrame({"X": [1, 2]}))

    assert loaded_session.task.pending_plan is None
    assert loaded_session.task.last_code is None
    assert loaded_session.task.status == "idle"


def test_switching_and_removing_a_dataset_also_reset_the_task(loaded_session: Session) -> None:
    loaded_session.add_dataset("second.csv", pd.DataFrame({"Y": [1]}), make_active=False)
    loaded_session.begin_turn("direct")
    loaded_session.end_turn(code="old")
    loaded_session.set_active("second.csv")
    assert loaded_session.task.last_code is None

    loaded_session.begin_turn("direct")
    loaded_session.end_turn(code="newer")
    loaded_session.remove_dataset("second.csv")
    assert loaded_session.task.last_code is None


def test_resetting_the_task_does_not_touch_conversation_or_data(loaded_session: Session) -> None:
    loaded_session.append_message("user", "hello")
    loaded_session.append_message("assistant", "hi", {"workflow": "converse"})
    loaded_session.reset_task()
    assert loaded_session.has_data
    assert len(loaded_session.history()) == 2


def test_the_last_task_digest_skips_conversational_replies(loaded_session: Session) -> None:
    loaded_session.append_message("user", "plot revenue")
    loaded_session.append_message(
        "assistant",
        "Here is the chart of revenue by month.",
        {
            "instruction": "plot revenue",
            "code": "plt.plot(df.A)",
            "workflow": "direct",
            "steps": [{"goal": "plot revenue", "code": "plt.plot(df.A)", "observation": "ok"}],
        },
    )
    loaded_session.append_message("user", "thanks")
    loaded_session.append_message("assistant", "Anytime.", {"workflow": "converse"})

    digest = loaded_session.last_task_digest()
    assert digest is not None
    assert digest["instruction"] == "plot revenue"
    assert digest["steps"] == ["plot revenue"]
    assert "chart" in digest["answer"]
    # ... while "a prior turn exists" is true either way, so a follow-up is possible.
    assert loaded_session.turn_context()["has_prior_turn"] is True


def test_rows_written_before_the_workflow_field_count_as_tasks(loaded_session: Session) -> None:
    loaded_session.append_message("assistant", "old answer", {"instruction": "q", "code": "x", "steps": []})
    digest = loaded_session.last_task_digest()
    assert digest is not None and digest["instruction"] == "q"


def test_no_task_digest_when_only_conversation_happened(loaded_session: Session) -> None:
    loaded_session.append_message("assistant", "Hello!", {"workflow": "converse"})
    assert loaded_session.last_task_digest() is None
    assert loaded_session.turn_context()["has_prior_turn"] is True
