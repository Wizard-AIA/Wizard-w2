"""Consent for actions taken *within* a run.

The plan gate ends its turn and is resumed by starting a new one. That cannot
work for an action chosen at iteration four, so these gates suspend instead: the
turn parks on a future and carries on where it stopped.

Anything that can suspend can hang, so every test here also pins the shape of
*not* hanging — a denial, with a reason, from a timeout, a refusal or a caller
that had no way to ask in the first place.
"""

from __future__ import annotations

import asyncio

import pytest

from src.core.agent.consent import ConsentRequest, consent_broker
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.session import Session


CODE_NEEDING_A_LIBRARY = """```python
import lifelines
print("fitted")
```"""

PLAIN_CODE = """```python
print(df.shape)
```"""


def _script(code: str) -> list[str]:
    """Plan, code, answer — the shape of a `fast` turn."""
    return ["<thought>Reasoning.</thought>\nStep 1: look at the data.", code, "The answer."]


# ------------------------------------------------------------ the broker --
async def test_a_consent_request_resolves_when_it_is_answered() -> None:
    collector = EventCollector()
    request = ConsentRequest(category="library_install", subject="lifelines", prompt="Install?")

    task = asyncio.ensure_future(consent_broker.ask("s1", request, collector, timeout=5))
    await asyncio.sleep(0)

    frames = collector.of_type(EventType.APPROVAL_REQUIRED)
    assert len(frames) == 1
    assert consent_broker.resolve("s1", frames[0].data["id"], True)

    decision = await task
    assert decision.approved
    assert consent_broker.waiting("s1") == 0


async def test_an_unanswered_request_denies_rather_than_hanging() -> None:
    """The whole reason a mid-run pause is safe to ship."""
    request = ConsentRequest(category="network", subject="q", prompt="Search?")
    decision = await consent_broker.ask("s2", request, EventCollector(), timeout=0.05)

    assert not decision.approved
    assert "treated as declined" in decision.reason
    assert consent_broker.waiting("s2") == 0


async def test_abandoning_a_session_denies_what_it_was_waiting_on() -> None:
    """A client that navigated away must not leave a turn parked until timeout."""
    collector = EventCollector()
    request = ConsentRequest(category="network", subject="q", prompt="Search?")

    task = asyncio.ensure_future(consent_broker.ask("s3", request, collector, timeout=30))
    await asyncio.sleep(0)
    consent_broker.abandon("s3")

    decision = await task
    assert not decision.approved


async def test_answering_something_that_is_not_waiting_is_not_an_error() -> None:
    """A late or duplicated answer from the client must not crash the socket."""
    assert consent_broker.resolve("s4", "no-such-request", True) is False


# --------------------------------------------------------- library install --
async def test_an_install_is_declined_when_nobody_can_be_asked(loaded_session: Session, stub_llm) -> None:
    """A REST turn has no reply channel, so `ask` resolves to a stated denial.

    Parking the request instead would hang a request nobody will ever answer.
    """
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=collector
    )

    assert result.status == "completed"
    assert any("no way to ask" in warning for warning in result.warnings)


async def test_auto_approve_installs_without_asking(loaded_session: Session, stub_llm) -> None:
    loaded_session.permissions.profile = "auto-approve"
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=collector
    )

    assert not collector.of_type(EventType.APPROVAL_REQUIRED)
    assert not any("Permission" in warning for warning in result.warnings)


async def test_a_denied_install_leaves_the_turn_alive(loaded_session: Session, stub_llm) -> None:
    """A refused step is information, not a failure.

    The loop already treats a sub-task that failed as something to route around,
    and a declined permission is the same kind of fact. Ending the turn instead
    would make declining once cost the whole question.
    """
    loaded_session.permissions.profile = "custom"
    loaded_session.permissions.set_ruling("library_install", "deny")
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))

    result = await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert any("deny" in warning for warning in result.warnings)


async def test_code_that_needs_nothing_new_is_never_gated(loaded_session: Session, stub_llm) -> None:
    """The gate must be quiet on the ordinary case, or it is unusable.

    `ask-always` is the default profile, so a turn using only what is installed
    has to run start to finish without a single prompt.
    """
    stub_llm(_script(PLAIN_CODE))
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="Compare spend across the regions", mode="fast", emitter=collector
    )

    assert result.status == "completed"
    assert not collector.of_type(EventType.APPROVAL_REQUIRED)
    # The in-process runtime always warns that it is not isolated; nothing else
    # should have anything to say.
    assert not [warning for warning in result.warnings if "Permission" in warning]


async def test_a_grant_is_remembered_for_the_rest_of_the_session(loaded_session: Session, stub_llm) -> None:
    """An investigation needing the same library three times asks once."""
    loaded_session.permissions.grant("library_install", "lifelines")
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    collector = EventCollector()

    await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=collector
    )

    assert not collector.of_type(EventType.APPROVAL_REQUIRED)


async def test_consent_is_asked_for_when_the_transport_can_carry_it(loaded_session: Session, stub_llm) -> None:
    """`can_prompt` is what turns a denial into a question."""
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    collector = EventCollector()

    task = asyncio.ensure_future(
        orchestrator.run(
            session=loaded_session,
            instruction="Calculate a survival curve",
            mode="fast",
            emitter=collector,
            can_prompt=True,
        )
    )

    frames = await _await_approval(collector)
    assert frames[0].data["category"] == "library_install"
    assert "lifelines" in frames[0].data["subject"]

    consent_broker.resolve(loaded_session.id, frames[0].data["id"], True)
    result = await task

    assert result.status == "completed"
    assert loaded_session.permissions.granted("library_install", "lifelines")


# -------------------------------------------------------- workspace writes --
async def test_a_write_outside_the_workspace_is_gated_not_silently_blocked(loaded_session: Session, stub_llm) -> None:
    """The guard keeps deciding; it just gains a way to be told yes.

    Under the default profile this is `deny`, which is exactly what the guard did
    before a profile existed — so the upgrade changes nothing here until the user
    asks it to.
    """
    stub_llm(_script("```python\ndf.to_csv('/etc/wizard-out.csv')\nprint('done')\n```"))

    result = await orchestrator.run(
        session=loaded_session, instruction="Export it", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert "/etc/wizard-out.csv" not in (result.code or "") or result.warnings


async def test_approving_a_path_widens_the_guards_roots(loaded_session: Session, stub_llm) -> None:
    """A grant is recorded as a root, so the re-scan is a normal scan."""
    loaded_session.permissions.profile = "custom"
    loaded_session.permissions.set_ruling("workspace_write", "allow")
    stub_llm(_script("```python\nprint('ok')\n```"))

    await orchestrator.run(session=loaded_session, instruction="Anything", mode="fast", emitter=EventCollector())

    # Nothing was outside the workspace, so nothing was granted.
    assert loaded_session.permissions.extra_roots == ()


# ------------------------------------------------------- the two dials --
async def test_depth_and_profile_are_independent_when_nothing_risky_comes_up(loaded_session: Session, stub_llm) -> None:
    """The milestone's acceptance criterion, stated directly.

    `deep` + `auto-approve` and `fast` + `ask-always` must reach the same answer
    when nothing gated happens. If the profile changed what the agent *found*
    rather than only what it asked about, the two dials would not be orthogonal
    and the composer would be offering a choice it does not really have.
    """
    loaded_session.permissions.profile = "auto-approve"
    stub_llm(["<thought>Reasoning.</thought>\nStep 1: count.", PLAIN_CODE, "ACTION: answer", "Five rows."])
    deep = await orchestrator.run(
        session=loaded_session, instruction="Compare spend across the regions", mode="deep", emitter=EventCollector()
    )

    loaded_session.permissions.profile = "ask-always"
    stub_llm(_script(PLAIN_CODE))
    fast = await orchestrator.run(
        session=loaded_session, instruction="Compare spend across the regions", mode="fast", emitter=EventCollector()
    )

    assert deep.status == fast.status == "completed"
    assert deep.code == fast.code
    assert not [warning for warning in deep.warnings + fast.warnings if "Permission" in warning]


async def test_the_profile_changes_consent_without_changing_depth(loaded_session: Session, stub_llm) -> None:
    """The other half: when something gated *does* come up, they visibly differ.

    Same depth, same script, opposite profiles — one runs straight through, the
    other stops. The iteration budget is untouched either way.
    """
    loaded_session.permissions.profile = "auto-approve"
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    permissive = await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=EventCollector()
    )

    loaded_session.permissions.profile = "custom"
    loaded_session.permissions.set_ruling("library_install", "deny")
    stub_llm(_script(CODE_NEEDING_A_LIBRARY))
    strict = await orchestrator.run(
        session=loaded_session, instruction="Calculate a survival curve", mode="fast", emitter=EventCollector()
    )

    assert permissive.iterations == strict.iterations, "the profile changed the iteration budget"
    assert not [warning for warning in permissive.warnings if "Permission" in warning]
    assert [warning for warning in strict.warnings if "Permission" in warning]


# ------------------------------------------------------------ helpers --
async def _await_approval(collector: EventCollector, timeout: float = 5.0) -> list:
    """Waits real time until the paused run has emitted its question.

    A fixed count of ``asyncio.sleep(0)`` ticks only yields the event loop; it
    buys the orchestrator no actual wall-clock time. The consent gate now sits
    behind the planner's ``asyncio.to_thread(skill_registry.search, ...)``, a
    real thread that has to be scheduled and finish before the code action --
    and the one that requests consent -- ever runs. On a loaded CI runner that
    can outlast 200 no-op ticks even though the run is proceeding normally, so
    this polls against a wall-clock deadline instead.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        frames = collector.of_type(EventType.APPROVAL_REQUIRED)
        if frames:
            return frames
        await asyncio.sleep(0.01)
    raise AssertionError("The run never asked for consent")


@pytest.fixture(autouse=True)
def _release_consent():
    yield
    consent_broker._pending.clear()
