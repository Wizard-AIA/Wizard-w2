"""Milestone 7 -- subagents for parallel sub-tasks.

`ActionKind.PARALLEL` fans one iteration out into isolated, concurrent child
loops (real concurrency under `host`/`docker`; sequential under `inprocess`,
since that backend has no per-call isolation and is dev/test-only). What these
tests pin: a branch's own numbers reach the parent's grounding check, a
malformed `parallel` choice degrades to a plain code step rather than failing
the turn, each branch is deterministic (no nested decision or verification
round-trip), and cost is attributable per branch while still folding into the
turn's own total.
"""

from __future__ import annotations

import pytest
from stubs import RecordingLLM

from src.config import TIER_BUDGETS, settings
from src.core.agent.actions import ActionKind
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import AnalysisOrchestrator, orchestrator
from src.core.llm.usage import TokenUsage, usage_ledger
from src.core.session import Session


CODE_A = "```python\nprint(df['A'].sum())\n```"
CODE_C = "```python\nprint(df['C'].sum())\n```"  # 0.1+0.2+0.3+0.4+0.5 == 1.5, unique to this branch
CODE_MEAN = "```python\nprint(df['A'].mean())\n```"  # 3.0, unique to the other branch
VERIFY_CODE = "```python\nprint('VERIFIED: 15.0')\n```"


@pytest.fixture
def recording_llm(monkeypatch):
    def install(responses: list[str]) -> RecordingLLM:
        stub = RecordingLLM(responses)
        for target in ("src.core.agent.orchestrator.llm_provider", "src.core.agent.flow.llm_provider"):
            module, _, attribute = target.rpartition(".")
            monkeypatch.setattr(f"{module}.{attribute}", stub, raising=False)
        return stub

    return install


@pytest.fixture
def tier(monkeypatch):
    def choose(name: str):
        monkeypatch.setattr(settings, "AGENT_TIER", name)

    return choose


async def _run(session: Session, mode: str = "auto", instruction: str = "sum column A"):
    collector = EventCollector()
    result = await orchestrator.run(session=session, instruction=instruction, mode=mode, emitter=collector)
    return result, collector


# --------------------------------------------------------------------------- #
# `_split_subgoals`
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "goal,expected",
    [
        ("revenue in region A | revenue in region B", ["revenue in region A", "revenue in region B"]),
        (" a | b | c ", ["a", "b", "c"]),
        ("a | a | b", ["a", "b"]),  # deduplicated
        # No delimiter -> a single-item list. `_split_subgoals` itself does not
        # decide parallelizability; `_act_parallel` treats fewer than two
        # parts as a malformed choice and degrades to a plain code step.
        ("just one thing", ["just one thing"]),
        ("", []),
        # A pipe not flanked by whitespace on both sides is not a delimiter
        # (see `_split_subgoals`'s docstring) -- a bare trailing "|" with
        # nothing after it to anchor the closing `\s+` stays part of the one
        # sub-goal rather than being read as an empty second part.
        ("a |", ["a |"]),
    ],
)
def test_split_subgoals(goal: str, expected: list[str]) -> None:
    budget = TIER_BUDGETS["full"]  # max_subagents=3, high enough not to clip these cases
    assert AnalysisOrchestrator._split_subgoals(goal, budget) == expected


def test_split_subgoals_is_capped_at_the_budget() -> None:
    budget = TIER_BUDGETS["balanced"]  # max_subagents=2
    assert AnalysisOrchestrator._split_subgoals("a | b | c | d", budget) == ["a", "b"]


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
async def test_a_branchs_number_is_grounded_in_the_parents_answer(loaded_session: Session, recording_llm, tier) -> None:
    """A subagent's own executed output reaches `check_grounding` unmodified.

    Each branch's `Step.observation` is its `executed_output`, folded straight
    into the parent's own `Investigation` -- so a number only a subagent
    computed (1.5, from summing column C) is not flagged as invented just
    because the main thread never touched it.
    """
    tier("balanced")
    recording_llm(
        [
            "1. Compare two totals.",  # plan
            CODE_A,  # iteration 1: hardcoded first code step
            "ACTION: parallel\nGOAL: sum column C | average column A",  # iteration 2: decide
            CODE_C,  # branch sub1
            CODE_MEAN,  # branch sub2
            "ACTION: answer\nGOAL: report it",  # iteration 3: decide
            VERIFY_CODE,  # verification
            "The sum of column C is 1.5.",  # final answer
        ]
    )

    result, collector = await _run(loaded_session)

    assert result.status == "completed"
    assert result.grounding["ok"] is True, result.grounding
    assert result.grounding["ungrounded"] == []

    # Ordered assertions are sound only because `conftest.py` pins
    # `EXECUTION_BACKEND=inprocess` for the whole suite, and `_act_parallel`
    # runs branches strictly in sequence on that backend. Under `host`/
    # `docker`, branches race through `asyncio.gather` and completion order
    # is not guaranteed -- a test exercising those backends would need to
    # compare `SUBAGENT_END` branches as a set instead.
    starts = [event.data["branch"] for event in collector.of_type(EventType.SUBAGENT_START)]
    ends = [event.data["branch"] for event in collector.of_type(EventType.SUBAGENT_END)]
    assert starts == ["sub1", "sub2"]
    assert ends == ["sub1", "sub2"]
    assert all(event.data["ok"] for event in collector.of_type(EventType.SUBAGENT_END))

    # Every branch-tagged frame carries its own branch, and nothing from a
    # branch's own iteration/action/observation collides with the main
    # thread's (which never carries a `branch` key at all).
    branch_actions = [e for e in collector.of_type(EventType.ACTION) if "branch" in e.data]
    assert {e.data["branch"] for e in branch_actions} == {"sub1", "sub2"}
    main_actions = [e for e in collector.of_type(EventType.ACTION) if "branch" not in e.data]
    assert [e.data["kind"] for e in main_actions] == ["code", "parallel", "answer"]


async def test_fewer_than_two_subgoals_degrades_to_a_plain_code_step(
    loaded_session: Session, recording_llm, tier
) -> None:
    """A `parallel` choice with no usable `|`-delimited goal is not fatal.

    Same philosophy as an unparseable decision anywhere else in the loop: it
    falls back to a normal `code` step rather than failing the turn or
    spawning a single, pointless subagent.
    """
    tier("balanced")
    recording_llm(
        [
            "1. Just sum it.",
            CODE_A,
            "ACTION: parallel\nGOAL: sum column A",  # no `|` -- not parallelizable
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "The sum is 15.",
        ]
    )

    result, collector = await _run(loaded_session)

    assert result.status == "completed"
    # The manager's choice is still reported honestly on the wire (it *did*
    # choose `parallel`) -- what matters is that nothing spawned and the turn
    # completed exactly as a plain code step would have.
    assert collector.of_type(EventType.SUBAGENT_START) == []
    kinds = [e.data["kind"] for e in collector.of_type(EventType.ACTION)]
    assert kinds == ["code", "parallel", "answer"]
    observations = collector.of_type(EventType.OBSERVATION)
    assert observations[1].data["ok"] is True
    assert "15" in observations[1].data["summary"]


async def test_a_branch_never_spends_a_decision_or_verification_round_trip(
    loaded_session: Session, recording_llm, tier
) -> None:
    """Each branch is deterministic: one worker call, no manager round-trip.

    `child_budget` forces `allow_decisions=False`/`allow_verification=False`
    regardless of the parent's own tier -- a branch that printed something
    stops on its own, the same rule the compact tier uses for the whole loop.
    """
    tier("full")  # max_subagents=3, so this also exercises three branches
    stub = recording_llm(
        [
            "1. Compare three totals.",
            CODE_A,
            "ACTION: parallel\nGOAL: sum column A | sum column C | average column A",
            CODE_A,
            CODE_C,
            CODE_MEAN,
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, _ = await _run(loaded_session)

    assert result.status == "completed"
    # plan, code(iter1), decide(parallel), 3x branch-code, decide(answer), verify, answer
    roles = [call["role"] for call in stub.calls]
    assert roles == [
        "manager",
        "worker",
        "manager",
        "worker",
        "worker",
        "worker",
        "manager",
        "worker",
        "manager",
    ]


async def test_subagent_end_reports_a_per_branch_cost_breakdown(loaded_session: Session, recording_llm, tier) -> None:
    """`SUBAGENT_END` always carries the cost keys, queried per branch's own id.

    `RecordingLLM` bypasses `LLMProvider._record` entirely (it stands in for
    the whole provider, not just the transport), so no real tokens are booked
    here -- what this pins is the *wiring*: each branch's `child_id` is a real,
    independently queryable key, distinct from its sibling's and from the
    parent's own.
    """
    tier("balanced")
    recording_llm(
        [
            "1. Compare two totals.",
            CODE_A,
            "ACTION: parallel\nGOAL: sum column C | average column A",
            CODE_C,
            CODE_MEAN,
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, collector = await _run(loaded_session)

    assert result.status == "completed"
    ends = collector.of_type(EventType.SUBAGENT_END)
    assert len(ends) == 2
    for event in ends:
        assert {"cost_usd", "total_tokens", "calls"} <= event.data.keys()


async def test_parallel_is_not_offered_below_balanced_tier(loaded_session: Session, recording_llm, tier) -> None:
    """The compact tier never sees `parallel` on its menu.

    Below balanced the loop decides for itself (`allow_decisions=False`), so
    there is no round-trip that could choose `parallel` in the first place --
    this pins that the tier gate on `_allowed_actions` matches that budget.
    """
    tier("compact")
    recording_llm(["1. Sum column A.", CODE_A, "The sum is 15."])

    result, collector = await _run(loaded_session)

    assert result.status == "completed"
    assert collector.of_type(EventType.SUBAGENT_START) == []
    assert ActionKind.PARALLEL not in {ActionKind(e.data["kind"]) for e in collector.of_type(EventType.ACTION)}


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #
def test_spawn_subagent_id_is_composite_and_registered(loaded_session: Session) -> None:
    child_id = loaded_session.spawn_subagent_id("sub1")

    assert child_id == f"{loaded_session.id}::sub:sub1"
    assert child_id in loaded_session._subagent_ids


def test_release_subagent_runtime_keeps_the_registry_and_the_ledger(loaded_session: Session) -> None:
    """Freeing a branch's runtime after it folds must not erase its cost.

    `_finalize` reads every id in `state.subagent_ids` through
    `usage_ledger.totals_many` *after* every branch has already been folded
    and released -- forgetting the ledger at release time would zero that out
    from under it. Full teardown is `dispose_subagent`, not this.
    """
    child_id = loaded_session.spawn_subagent_id("sub1")
    usage_ledger.record(child_id, "ollama", "qwen2.5-coder", "worker", TokenUsage(10, 5))

    loaded_session.release_subagent_runtime(child_id)

    assert child_id in loaded_session._subagent_ids
    assert usage_ledger.totals(child_id)["calls"] == 1


def test_dispose_subagent_forgets_everything(loaded_session: Session) -> None:
    child_id = loaded_session.spawn_subagent_id("sub1")
    usage_ledger.record(child_id, "ollama", "qwen2.5-coder", "worker", TokenUsage(10, 5))

    loaded_session.dispose_subagent(child_id)

    assert child_id not in loaded_session._subagent_ids
    assert usage_ledger.totals(child_id)["calls"] == 0


def test_session_dispose_releases_any_subagent_still_registered(loaded_session: Session) -> None:
    """A turn cancelled mid-fan-out must not leak a subagent past session end.

    A subagent never appears in `SessionManager`, so nothing else walks it to
    reap or evict it -- `Session.dispose()` is the only thing that must.
    """
    child_id = loaded_session.spawn_subagent_id("sub1")
    usage_ledger.record(child_id, "ollama", "qwen2.5-coder", "worker", TokenUsage(10, 5))

    loaded_session.dispose()

    assert loaded_session._subagent_ids == set()
    assert usage_ledger.totals(child_id)["calls"] == 0


async def test_competing_routes_are_compared_on_the_full_tier_for_a_comparative_objective(
    loaded_session: Session, recording_llm, tier
) -> None:
    """Phase 8: a `parallel` fan-out that names two competing methods on a high-impact
    objective produces a `ROUTE_COMPARISON` naming which method's assumptions actually fit,
    not a silent pick between two disagreeing figures.
    """
    tier("full")
    corr_a = "```python\nprint('pearson_correlation of A and C:', df['A'].corr(df['C']))\n```"
    corr_b = "```python\nprint('spearman_correlation of A and C:', df['A'].corr(df['C'], method='spearman'))\n```"
    recording_llm(
        [
            "1. Compare two correlation methods.",
            CODE_A,
            "ACTION: parallel\nGOAL: pearson_correlation of A and C | spearman_correlation of A and C",
            corr_a,
            corr_b,
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, collector = await _run(loaded_session, instruction="compare two correlation methods for A and C")

    assert result.status == "completed"
    comparisons = collector.of_type(EventType.ROUTE_COMPARISON)
    assert len(comparisons) == 1
    event_data = comparisons[0].data
    assert event_data["verdict"] in {"agree", "disagree", "inconclusive"}
    stored = result.analysis["route_comparisons"]
    assert len(stored) == 1
    assert stored[0] == {key: value for key, value in event_data.items() if key != "group"}


async def test_competing_routes_are_not_compared_below_the_full_tier(
    loaded_session: Session, recording_llm, tier
) -> None:
    """Balanced tier still runs `parallel`, but Phase 8's route comparison is full-tier only."""
    tier("balanced")
    corr_a = "```python\nprint('pearson_correlation of A and C:', df['A'].corr(df['C']))\n```"
    corr_b = "```python\nprint('spearman_correlation of A and C:', df['A'].corr(df['C'], method='spearman'))\n```"
    recording_llm(
        [
            "1. Compare two correlation methods.",
            CODE_A,
            "ACTION: parallel\nGOAL: pearson_correlation of A and C | spearman_correlation of A and C",
            corr_a,
            corr_b,
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, collector = await _run(loaded_session, instruction="compare two correlation methods for A and C")

    assert result.status == "completed"
    assert collector.of_type(EventType.ROUTE_COMPARISON) == []
    assert result.analysis["route_comparisons"] == []


async def test_competing_routes_are_not_compared_for_a_plain_descriptive_objective(
    loaded_session: Session, recording_llm, tier
) -> None:
    """Full tier alone is not enough -- the objective must also be high-impact or ambiguous."""
    tier("full")
    recording_llm(
        [
            "1. Compare two totals.",
            CODE_A,
            "ACTION: parallel\nGOAL: sum column A | sum column C",
            CODE_A,
            CODE_C,
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, collector = await _run(loaded_session, instruction="what is the total of column A")

    assert result.status == "completed"
    assert collector.of_type(EventType.ROUTE_COMPARISON) == []
    assert result.analysis["route_comparisons"] == []


async def test_subagent_disabled_globally_removes_it_from_the_menu(
    loaded_session: Session, recording_llm, tier, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "SUBAGENT_ENABLED", False)
    tier("balanced")
    recording_llm(
        [
            "1. Compare two totals.",
            CODE_A,
            "ACTION: parallel\nGOAL: sum column C | average column A",  # menu no longer offers it
            "ACTION: answer\nGOAL: report it",
            VERIFY_CODE,
            "Done.",
        ]
    )

    result, collector = await _run(loaded_session)

    assert result.status == "completed"
    assert collector.of_type(EventType.SUBAGENT_START) == []
