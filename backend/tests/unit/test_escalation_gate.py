"""The escalation sentinel means "hand this turn to the analysis loop" only when it is the whole reply."""

from __future__ import annotations

import pytest

from src.core.agent.conversation import EscalationGate
from src.core.agent.routing import ESCALATE_SENTINEL


def run(chunks: list[str], enabled: bool = True) -> tuple[str, bool]:
    gate = EscalationGate(enabled)
    shown = "".join(gate.feed(chunk) for chunk in chunks) + gate.flush()
    return shown, gate.escalated


def test_the_sentinel_alone_escalates_and_shows_nothing() -> None:
    assert run([ESCALATE_SENTINEL]) == ("", True)


def test_surrounding_whitespace_does_not_matter() -> None:
    assert run(["\n  ", ESCALATE_SENTINEL, " \n"]) == ("", True)


@pytest.mark.parametrize("cut", range(1, len(ESCALATE_SENTINEL)))
def test_a_sentinel_split_at_any_point_still_escalates(cut: int) -> None:
    assert run([ESCALATE_SENTINEL[:cut], ESCALATE_SENTINEL[cut:]]) == ("", True)


def test_the_sentinel_followed_by_text_is_text_not_an_escalation() -> None:
    """A model echoing the marker from attacker-controlled text must not start an analysis."""
    shown, escalated = run([ESCALATE_SENTINEL + " Sure, here is a joke."])
    assert not escalated
    assert shown == "Sure, here is a joke."


def test_text_arriving_after_the_marker_in_a_later_chunk_is_released() -> None:
    shown, escalated = run([ESCALATE_SENTINEL, "\n", "Actually, hello!"])
    assert not escalated
    assert shown == "Actually, hello!"


def test_a_marker_inside_a_longer_reply_is_shown_as_written() -> None:
    shown, escalated = run(["Hello! ", ESCALATE_SENTINEL])
    assert not escalated
    assert shown == "Hello! " + ESCALATE_SENTINEL


def test_a_reply_that_stops_short_of_the_marker_is_text() -> None:
    assert run(["[[NEEDS"]) == ("[[NEEDS", False)


def test_an_ordinary_reply_is_released_in_full() -> None:
    assert run(["Hi", " there", "!"]) == ("Hi there!", False)


def test_a_disabled_gate_is_transparent_even_for_the_sentinel() -> None:
    assert run([ESCALATE_SENTINEL], enabled=False) == (ESCALATE_SENTINEL, False)
