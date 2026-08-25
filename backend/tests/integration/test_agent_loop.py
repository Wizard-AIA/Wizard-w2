"""The observe -> decide -> act loop, driven by a scripted model.

These are the tests that distinguish the new architecture from the old one. The
previous orchestrator fixed a plan before touching the data and executed it step
by step; what is proved here is that the agent now chooses each next move from
real execution output, revises its plan when the data disagrees with it, and
stops when it decides it has the answer.
"""

from __future__ import annotations

import pandas as pd
import pytest
from stubs import ScriptedLLM

from src.config import settings
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.analysis.provenance import EvidenceGraph
from src.core.database import db_mgr
from src.core.ingest.documents import ContextDocument, DocumentChunk
from src.core.session import Session
from src.core.skills.registry import skill_registry
from src.core.tools.catalog import CatalogEngine


def kinds(collector: EventCollector) -> list[str]:
    return [event.type.value for event in collector.events]


def actions(collector: EventCollector) -> list[str]:
    return [event.data["kind"] for event in collector.of_type(EventType.ACTION)]


# --------------------------------------------------------------------------- #
# Iterating
# --------------------------------------------------------------------------- #
async def test_the_agent_runs_several_dependent_steps(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """The whole point: step two is chosen after seeing step one's real output."""
    stub_llm(
        [
            "1. Investigate the data",  # opening plan
            "```python\nprint('sum', df['A'].sum())\n```",  # iteration 1 (never asked)
            "ACTION: code\nGOAL: now compute the mean",  # iteration 2 decision
            "```python\nprint('mean', df['A'].mean())\n```",
            "ACTION: answer\nGOAL: report both",  # iteration 3 decision
            "```python\nprint('VERIFIED: ok')\n```",  # verification
            "The sum is 15 and the mean is 3.",  # answer
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="summarise column A", mode="auto", emitter=collector
    )

    assert result.status == "completed"
    assert result.iterations == 3
    assert actions(collector) == ["code", "code", "answer"]
    # Both executions are in the record the answer was written from.
    assert "15" in result.answer


async def test_the_first_iteration_is_not_put_to_the_model(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """There is nothing to observe yet, so asking costs a round-trip to be told
    what is already known: write the code."""
    stub = stub_llm(
        [
            "1. Count the rows",
            "```python\nprint(len(df))\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: 5')\n```",
            "There are 5 rows.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="how big", mode="auto", emitter=collector)

    first_action = collector.of_type(EventType.ACTION)[0]
    assert first_action.data["kind"] == "code"
    # plan, code, decision, verification, answer — no decision before the first code.
    assert len(stub.prompts) == 5


async def test_the_budget_forces_an_answer(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """A model that never chooses `answer` must not loop until the ceiling.

    On the final permitted iteration the choice is made for it, so a run always
    terminates with something rather than being cut off mid-investigation.
    """
    stub_llm(
        ["1. Explore"] + ["ACTION: code\nGOAL: keep going", "```python\nprint('again')\n```"] * 30 + ["Partial answer."]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="explore forever", mode="auto", emitter=collector
    )

    assert result.status == "completed"
    budget = settings.budget_for("auto", None)
    assert result.iterations <= budget.iterations
    assert actions(collector)[-1] == "answer"


async def test_iteration_frames_report_the_budget(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """The UI shows progress against a budget, so the budget has to be on the wire."""
    stub_llm(["1. Go", "```python\nprint(1)\n```", "ACTION: answer\nGOAL: done", "```python\npass\n```", "Done."])
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="anything", mode="auto", emitter=collector)

    starts = collector.of_type(EventType.ITERATION_START)
    assert starts
    assert starts[0].data["n"] == 1
    assert starts[0].data["budget"] >= 1
    assert starts[0].data["mode"] == "auto"


# --------------------------------------------------------------------------- #
# Carrying results forward
# --------------------------------------------------------------------------- #
async def test_full_prior_output_reaches_the_next_step(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """The old pipeline passed 200 characters between steps, so a later step
    could not see the values an earlier one computed. This is that regression."""
    marker = "DISTINCTIVE_VALUE_" + "y" * 400
    stub = stub_llm(
        [
            "1. Two steps",
            f"```python\nprint('{marker}')\n```",
            "ACTION: code\nGOAL: use the value above",
            "```python\nprint('second')\n```",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "Done.",
        ]
    )

    await orchestrator.run(session=loaded_session, instruction="chain", mode="auto", emitter=EventCollector())

    # The prompt that generated the *second* block must contain the first's output.
    second_code_prompt = stub.prompts[3]
    assert marker in second_code_prompt


async def test_a_failed_step_does_not_end_the_investigation(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """A sub-task that fails is information; the agent can route around it."""
    stub_llm(
        [
            "1. Try something",
            "```python\nprint(df['NOPE'].sum())\n```",  # KeyError, retried
            "```python\nprint(df['NOPE'].sum())\n```",
            "```python\nprint(df['NOPE'].sum())\n```",
            "```python\nprint(df['NOPE'].sum())\n```",
            "ACTION: code\nGOAL: use a column that exists",
            "```python\nprint(df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: 15')\n```",
            "The sum is 15.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="sum it", mode="auto", emitter=collector)

    assert result.status == "completed"
    assert "15" in result.answer
    failed = [event for event in collector.of_type(EventType.OBSERVATION) if not event.data["ok"]]
    assert failed, "the failure should have been recorded as an observation"


# --------------------------------------------------------------------------- #
# Individual actions
# --------------------------------------------------------------------------- #
async def test_inspect_costs_no_model_call(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Schema and null structure are facts about a frame. Making the agent write
    and run code to discover them would cost a generation plus an execution."""
    stub = stub_llm(
        [
            "1. Look first",
            "```python\nprint('start')\n```",
            "ACTION: inspect\nGOAL: describe the columns",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="what is in here", mode="auto", emitter=collector)

    assert "inspect" in actions(collector)
    observation = collector.of_type(EventType.OBSERVATION)[1]
    # Real schema content, produced without a worker round-trip.
    assert "dtype" in observation.data["summary"]
    assert "A" in observation.data["summary"]
    assert len(stub.prompts) == 6


async def test_inspect_surfaces_a_dirty_join_key_across_loaded_tables(session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 4's acceptance criterion for the join-key detector, exercised through the loop
    rather than the module directly: `orders.customer_id` is an int, `customers.customer_id`
    is the same values zero-padded as strings -- a merge on it would silently drop every row.
    """
    session.add_dataset("orders.csv", pd.DataFrame({"customer_id": [1, 2, 3], "amount": [5, 7, 9]}))
    session.add_dataset("customers.csv", pd.DataFrame({"customer_id": ["01", "02", "03"], "name": ["a", "b", "c"]}))
    stub_llm(
        [
            "1. Look first",
            "```python\nprint('start')\n```",
            "ACTION: inspect\nGOAL: describe the columns",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=session, instruction="join them", mode="auto", emitter=collector)

    observation = collector.of_type(EventType.OBSERVATION)[1]
    assert "Dirty join key" in observation.data["summary"]
    assert result.analysis["understanding"]["join_keys"][0]["dirty"] is True


async def test_reflect_revises_the_plan_and_says_so(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """The plan is a living document. A revision is not a retry, and the UI has
    to be able to tell them apart."""
    stub_llm(
        [
            "1. Original plan",
            "```python\nprint('surprising result')\n```",
            "ACTION: reflect\nGOAL: rethink",
            "Column A is not what I assumed.\n1. New first step\n2. New second step",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="analyse", mode="auto", emitter=collector)

    revisions = collector.of_type(EventType.PLAN_REVISED)
    assert revisions
    assert "New first step" in revisions[0].data["plan"]
    assert revisions[0].data["previous"] == "1. Original plan"
    assert "New first step" in result.plan


async def test_plan_revision_history_is_appended_not_overwritten(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """ADR 0002: a reflection appends a revision; it does not erase the one before it."""
    stub_llm(
        [
            "1. Original plan",
            "```python\nprint('surprising result')\n```",
            "ACTION: reflect\nGOAL: rethink",
            "Column A is not what I assumed.\n1. New first step\n2. New second step",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="analyse", mode="auto", emitter=collector)

    revisions = result.analysis["plan"]["revisions"]
    assert len(revisions) == 2
    assert revisions[0]["text"] == "1. Original plan"
    assert "New first step" in revisions[1]["text"]
    assert revisions[0]["index"] == 0
    assert revisions[1]["index"] == 1
    assert result.analysis["plan"]["intended_analyses"] == ["New first step", "New second step"]

    stored = db_mgr.get_plan_revisions(result.message_id)
    assert [row["text"] for row in stored] == [row["text"] for row in revisions]


async def test_reflection_is_withheld_from_the_compact_tier(loaded_session: Session, stub_llm, monkeypatch) -> None:  # noqa: F811
    """A 1.5B model reliably spends the iteration restating the question."""
    monkeypatch.setattr(settings, "AGENT_TIER", "compact")
    stub_llm(
        [
            "1. Go",
            "```python\nprint(1)\n```",
            "ACTION: reflect\nGOAL: rethink",  # asked for, but not on the menu
            "```python\nprint(2)\n```",
            "ACTION: answer\nGOAL: done",
            "Done.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="analyse", mode="auto", emitter=collector)

    assert "reflect" not in actions(collector)


async def test_consult_retrieves_from_attached_documents(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """The capability hard questions actually turn on: cross-referencing the
    tables against a definition that exists only in a document."""
    document = ContextDocument(name="rules.md", text="Status C means cancelled, not complete.")
    document.chunks.append(DocumentChunk(document="rules.md", index=0, text="Status C means cancelled, not complete."))
    loaded_session.add_document(document)

    stub_llm(
        [
            "1. Check the meaning of the status codes",
            "```python\nprint('start')\n```",
            "ACTION: consult\nGOAL: what does status C mean",
            "ACTION: answer\nGOAL: report",
            "```python\npass\n```",
            "C means cancelled.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(
        session=loaded_session, instruction="what does status C mean", mode="auto", emitter=collector
    )

    assert "consult" in actions(collector)
    consulted = collector.of_type(EventType.OBSERVATION)[1].data["summary"]
    assert "cancelled" in consulted
    assert "rules.md" in consulted


async def test_consult_is_not_offered_with_nothing_to_consult(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """An action that cannot succeed must not be on the menu — a small model
    will pick it, waste the iteration and learn nothing.

    ``consult`` now has two corpora, so this needs both empty: no documents
    attached, and no skills installed (the suite pins both derived skill roots to
    empty temp directories).
    """
    stub_llm(
        [
            "1. Go",
            "```python\nprint(1)\n```",
            "ACTION: consult\nGOAL: look it up",
            "```python\nprint(2)\n```",
            "ACTION: answer\nGOAL: done",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="analyse", mode="auto", emitter=collector)

    assert "consult" not in actions(collector)


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
@pytest.fixture
def installed_skill():
    """One skill in the user layer, gone again afterwards."""
    skill_registry.write(
        "cohort-method",
        "How to compute cohort retention",
        "Anchor the cohort on the first purchase date, never on a recurring event.",
    )
    yield skill_registry.get("cohort-method")
    skill_registry.delete("cohort-method")


async def test_a_matching_skill_reaches_the_planning_prompt(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """The mechanism behind the milestone: know-how the user wrote informs the
    plan, without an extra round-trip to discover it."""
    stub = stub_llm(["1. Compute retention", "```python\nprint(1)\n```", "```python\npass\n```", "Done."])
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session,
        instruction="compute cohort retention for the signup cohorts",
        mode="fast",
        emitter=collector,
    )

    assert "<skills>" in stub.prompts[0]
    assert "Anchor the cohort on the first purchase date" in stub.prompts[0]
    assert result.skills_used == ["cohort-method"]


async def test_the_skill_is_named_on_a_frame_not_just_in_a_prompt(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """Acceptance criterion 1. A prompt nobody sees cannot be how the agent
    "names which skill informed a decision"."""
    stub_llm(["1. Go", "```python\nprint(1)\n```", "```python\npass\n```", "Done."])
    collector = EventCollector()

    await orchestrator.run(
        session=loaded_session, instruction="cohort retention by signup date", mode="fast", emitter=collector
    )

    frames = collector.of_type(EventType.SKILL)
    assert [frame.data["name"] for frame in frames] == ["cohort-method"]
    assert frames[0].data["layer"] == "user"
    assert frames[0].data["score"] > 0


async def test_an_unrelated_question_gets_no_skill_block(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """Prompt budget is only spent when something actually matched."""
    stub = stub_llm(["1. Go", "```python\nprint(1)\n```", "```python\npass\n```", "Done."])

    result = await orchestrator.run(
        session=loaded_session, instruction="what is the capital of France", mode="fast", emitter=EventCollector()
    )

    assert "<skills>" not in stub.prompts[0]
    assert result.skills_used == []


async def test_consult_is_offered_with_skills_and_no_documents(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """The usual shape of a fresh install: nothing uploaded, skills present.

    Before this, ``consult`` was gated on documents alone, so the installed
    skills were unreachable through the action the milestone says they should be
    consulted by.
    """
    stub_llm(
        [
            "1. Go",
            "```python\nprint(1)\n```",
            "ACTION: consult\nGOAL: how do I anchor a cohort",
            "ACTION: answer\nGOAL: done",
            "```python\npass\n```",
            "Done.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="cohort retention analysis", mode="auto", emitter=collector
    )

    assert "consult" in actions(collector)
    consulted = collector.of_type(EventType.OBSERVATION)[1].data["summary"]
    assert "From skill `cohort-method`" in consulted
    assert "first purchase date" in consulted
    assert "cohort-method" in result.skills_used


async def test_a_recurring_analysis_is_offered_for_promotion(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Nothing is written — the frame is an offer, and the file only appears when
    the user confirms."""
    collector = EventCollector()
    for _ in range(settings.SKILL_PROMOTION_THRESHOLD):
        stub_llm(["1. Go", "```python\nprint('total', 42)\n```", "```python\npass\n```", "The total is 42."])
        collector = EventCollector()
        await orchestrator.run(
            session=loaded_session,
            instruction="break the revenue down by region",
            mode="fast",
            emitter=collector,
        )

    offers = collector.of_type(EventType.SKILL_CANDIDATE)
    assert len(offers) == 1
    assert offers[0].data["occurrences"] == settings.SKILL_PROMOTION_THRESHOLD
    assert offers[0].data["kind"] == "recurring"
    assert skill_registry.get(offers[0].data["suggested_name"]) is None


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #
async def test_an_unparseable_decision_does_not_break_the_run(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """A model that cannot follow the format still completes the analysis."""
    stub_llm(
        [
            "1. Go",
            "```python\nprint('one')\n```",
            "########## !!!! ##########",  # nothing readable
            "```python\nprint('two')\n```",
            "ACTION: answer\nGOAL: done",
            "```python\npass\n```",
            "Finished.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="analyse", mode="auto", emitter=collector)

    assert result.status == "completed"
    inferred = [event for event in collector.of_type(EventType.ACTION) if event.data["inferred"]]
    assert inferred, "a guessed action must be reported as guessed"


async def test_losing_the_model_mid_loop_keeps_the_work(loaded_session: Session, monkeypatch) -> None:
    """A daemon that dies on iteration three must not discard iterations one and two."""
    from src.core.llm.provider import LLMUnavailableError

    class FailsAfter(ScriptedLLM):
        async def acomplete(self, prompt: str, **_: object) -> str:
            if not self.responses:
                raise LLMUnavailableError("connection refused")
            return await super().acomplete(prompt)

        async def stream_to(self, prompt: str, on_delta=None, **_: object) -> str:
            if not self.responses:
                raise LLMUnavailableError("connection refused")
            return await super().stream_to(prompt, on_delta)

    stub = FailsAfter(["1. Go", "```python\nprint('real output here')\n```"])
    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", stub)

    result = await orchestrator.run(
        session=loaded_session, instruction="analyse", mode="auto", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert "real output here" in result.answer, "the completed work should have been reported"


# --------------------------------------------------------------------------- #
# Rigor
# --------------------------------------------------------------------------- #
async def test_a_verification_mismatch_is_surfaced(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """A wrong join grain produces a confident, plausible, wrong number that no
    self-review catches — because the reviewer is the model that made it."""
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('MISMATCH: got 15 expected 99')\n```",
            "The total is 15.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total of A", mode="auto", emitter=collector)

    verifications = collector.of_type(EventType.VERIFICATION)
    assert verifications
    assert verifications[0].data["status"] == "mismatch"
    assert any("not trustworthy" in warning for warning in result.warnings)
    assert any(v["validator"] == "computational" and v["severity"] == "error" for v in result.analysis["validations"])


async def test_unseeded_sampling_is_flagged_by_the_reproducibility_validator(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 6's validation framework runs alongside `_verify`, not only inside it -- a validator
    with nothing to do with recomputation still fires from the same turn's evidence."""
    stub_llm(
        [
            "1. Sample",
            "```python\nprint(df.sample(2))\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Here is a sample.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="show a sample", mode="auto", emitter=collector)

    assert any(
        v["validator"] == "reproducibility" and "not be exactly reproducible" in v["message"]
        for v in result.analysis["validations"]
    )


async def test_a_simpsons_paradox_is_caught_and_reaches_the_answer_prompt(session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 7's acceptance criterion: a Simpson's-paradox fixture produces a finding the agent
    acts on -- here, by seeing it in the prompt that writes the final answer (Rule 4: the critic
    itself never edits `state.output`)."""
    counts = [
        ("A", "small", 1, 81),
        ("A", "small", 0, 6),
        ("A", "large", 1, 192),
        ("A", "large", 0, 71),
        ("B", "small", 1, 234),
        ("B", "small", 0, 36),
        ("B", "large", 1, 55),
        ("B", "large", 0, 25),
    ]
    rows = [
        {"treatment": treatment, "stone_size": size, "success": outcome}
        for treatment, size, outcome, n in counts
        for _ in range(n)
    ]
    session.add_dataset("treatments.csv", pd.DataFrame(rows))
    stub = stub_llm(
        [
            "1. Compute the overall success rate",
            "```python\nprint(df['success'].mean())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Treatment B has the higher success rate overall.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=session, instruction="which treatment works better", mode="auto", emitter=collector
    )

    critic_events = collector.of_type(EventType.CRITIC_FINDING)
    assert any(event.data["category"] == "simpsons_paradox" for event in critic_events)
    assert any(f["category"] == "simpsons_paradox" for f in result.analysis["critic_findings"])
    answer_prompt = stub.prompts[-1]
    assert "<critic_findings>" in answer_prompt
    assert "reverses once segmented" in answer_prompt


async def test_confidence_verdict_is_reported_after_verification(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 9: confidence is computed and reported alongside verification, never left implicit."""
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "The total is correct.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total of A", mode="auto", emitter=collector)

    confidence_events = collector.of_type(EventType.CONFIDENCE)
    assert confidence_events
    assert result.analysis["confidence"]["verdict"] == confidence_events[-1].data["verdict"]
    assert "stop" in result.analysis["confidence"]


async def test_a_dataset_lacking_the_evidence_produces_cannot_answer_with_named_reasons(
    session: Session,
    stub_llm,  # noqa: F811
) -> None:
    """Phase 9's acceptance criterion: a dataset too sparse to support the claim, combined with a
    disagreeing recomputation, produces `cannot_answer` -- a success state naming the gap, never a
    confident guess presented as settled."""
    sparse = pd.DataFrame({"value": [1.0, None, None, None, None]})
    session.add_dataset("sparse.csv", sparse, profile=CatalogEngine.analyze(sparse))
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['value'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('MISMATCH: got 1 expected 99')\n```",
            "The total is 1.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=session, instruction="total of value", mode="auto", emitter=collector)

    confidence_events = collector.of_type(EventType.CONFIDENCE)
    assert confidence_events[-1].data["verdict"] == "cannot_answer"
    assert result.analysis["confidence"]["verdict"] == "cannot_answer"
    assert len(result.analysis["confidence"]["reasons"]) >= 3


async def test_finalize_captures_an_immutable_run_snapshot(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 10: `_finalize` captures an `AnalysisRun` alongside the mutable analysis-state
    snapshot -- what a later export or report renders from (ADR 0005)."""
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "The total is correct.",
        ]
    )

    result = await orchestrator.run(
        session=loaded_session, instruction="total of A", mode="auto", emitter=EventCollector()
    )

    run = db_mgr.get_analysis_run(result.message_id)
    assert run is not None
    assert run["instruction"] == "total of A"
    assert run["answer"] == result.answer
    assert run["dataset_manifest"]
    assert run["dataset_manifest"][0]["content_hash"]
    assert run["steps"]


async def test_a_captured_run_renders_in_all_three_report_modes(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 12: a real turn's captured run renders through every mode with no error, and its
    claims -- the answer's own grounded figures -- carry a real provenance chain, not a guess."""
    from src.core.analysis.reports import build, render
    from src.core.analysis.runs import AnalysisRun

    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "The total is 15.",
        ]
    )

    result = await orchestrator.run(
        session=loaded_session, instruction="total of A", mode="auto", emitter=EventCollector()
    )

    run = AnalysisRun.from_dict(db_mgr.get_analysis_run(result.message_id))
    model = build(run)
    assert model.claims, "the answer's grounded figure should have become a claim node"
    assert model.every_claim_has_provenance

    for mode in ("executive", "research", "technical"):
        report = render(run, mode=mode)
        assert "total of A" in report


async def test_evidence_graph_traces_a_claim_to_its_execution_and_dataset(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """Phase 3's acceptance criterion: every grounded figure traces to an execution and a
    dataset version -- not asserted from logs, but from the graph itself.
    """
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: 15')\n```",
            "The total is 15.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total of A", mode="auto", emitter=collector)

    evidence = result.analysis["evidence"]
    assert {"dataset", "code", "execution", "validation", "claim"} <= {node["kind"] for node in evidence["nodes"]}

    graph = EvidenceGraph.from_dict(evidence)
    claim_id = next(node.id for node in graph.nodes.values() if node.kind == "claim" and node.label == "15")
    traced_kinds = {node.kind for node in graph.trace(claim_id)}
    assert {"dataset", "code", "execution", "claim"} <= traced_kinds

    assert db_mgr.get_evidence_graph(result.message_id) == evidence


async def test_fast_mode_skips_verification(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    stub = stub_llm(["1. Go", "```python\nprint(1)\n```", "The answer."])

    await orchestrator.run(session=loaded_session, instruction="quick", mode="fast", emitter=EventCollector())

    # plan, code, answer. Nothing else.
    assert len(stub.prompts) == 3


async def test_an_invented_figure_is_flagged(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    stub_llm(
        [
            "1. Compute",
            "```python\nprint('total', df['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: 15')\n```",
            "The total is 15, which is 87.4% above last quarter.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total of A", mode="auto", emitter=collector)

    assert not result.grounding["ok"]
    assert "87.4" in result.grounding["ungrounded"]
    assert any("unverified" in warning for warning in result.warnings)
    # The answer itself is untouched: editing model output after the fact is how
    # legitimate results were deleted last time.
    assert "87.4" in result.answer


async def test_code_assumptions_are_recorded(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    stub_llm(
        [
            "1. Compute",
            "```python\nprint(df.dropna()['A'].sum())\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "The total is 15.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total", mode="auto", emitter=collector)

    assert any("excluded" in note for note in result.assumptions)
    assert collector.of_type(EventType.ASSUMPTION)


async def test_a_reproducible_script_is_written(loaded_session: Session, stub_llm) -> None:  # noqa: F811
    """An answer is a one-off; a script is an asset that survives the question."""
    stub_llm(
        [
            "1. Two things",
            "```python\nprint('first')\n```",
            "ACTION: code\nGOAL: second thing",
            "```python\nprint('second')\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Done.",
        ]
    )

    result = await orchestrator.run(
        session=loaded_session, instruction="analyse", mode="auto", emitter=EventCollector()
    )

    script = loaded_session.workspace / "analysis.py"
    assert script.exists()
    body = script.read_text(encoding="utf-8")
    assert "print('first')" in body
    assert "print('second')" in body
    assert "analyse" in body, "the script should record the question it answers"
    assert any(artifact.get("name") == "analysis.py" for artifact in result.artifacts)


async def test_the_reproducible_script_binds_every_table(session: Session, stub_llm) -> None:  # noqa: F811
    """Milestone 9: the always-on script used to load a single flat
    `dataset.csv` unconditionally, so a second table's name was never bound
    and the script `NameError`d the moment it ran standalone. It must now
    bind every table the session actually has."""
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1, 2], "customer_id": [10, 11]}))
    session.add_dataset("customers.csv", pd.DataFrame({"customer_id": [10, 11], "name": ["a", "b"]}))
    stub_llm(
        [
            "1. Join them",
            "```python\nprint(tables['orders'].merge(tables['customers']))\n```",
            "ACTION: answer\nGOAL: report",
            "```python\nprint('VERIFIED: ok')\n```",
            "Done.",
        ]
    )

    await orchestrator.run(session=session, instruction="join", mode="auto", emitter=EventCollector())

    body = (session.workspace / "analysis.py").read_text(encoding="utf-8")
    assert "tables/orders.feather" in body
    assert "tables/customers.feather" in body


async def test_the_reproducible_script_looks_up_a_connector_table_by_name(session: Session, stub_llm) -> None:  # noqa: F811
    """A connector-sourced table must never be embedded or carry a secret --
    `ConnectionStore.by_name` exists for exactly this."""
    from src.core.connectors.spec import ConnectionSpec
    from src.core.connectors.store import connection_store

    spec = ConnectionSpec(name="Shop", kind="relational", options={"driver": "sqlite", "database": "x.db"})
    connection_store.save(spec, secret="s3cr3t-password")
    try:
        handle = session.add_dataset("orders", pd.DataFrame({"id": [1]}))
        handle.origin = "Shop"
        handle.profile["target"] = "orders"
        stub_llm(
            [
                "1. Count",
                "```python\nprint(len(df))\n```",
                "ACTION: answer\nGOAL: report",
                "```python\nprint('VERIFIED: ok')\n```",
                "Done.",
            ]
        )

        await orchestrator.run(session=session, instruction="count", mode="auto", emitter=EventCollector())

        body = (session.workspace / "analysis.py").read_text(encoding="utf-8")
        assert "connection_store.by_name('Shop')" in body
        assert "s3cr3t-password" not in body
    finally:
        # Otherwise the saved spec and its credential outlive this test and
        # leak into whatever runs next.
        connection_store.delete(spec.id)


# --------------------------------------------------------------------------- #
# Multiple tables
# --------------------------------------------------------------------------- #
def test_every_table_is_exposed_to_generated_code(session: Session) -> None:
    """Hard questions need two or more sources. Previously only the active frame
    was loaded and the rest were merely named in the prompt as files the model
    might choose to read — which it usually did not, and got wrong when it did."""
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1, 2], "customer_id": [10, 11]}))
    session.add_dataset("customers.csv", pd.DataFrame({"customer_id": [10, 11], "name": ["a", "b"]}))

    assert set(session.tables) == {"orders", "customers"}
    assert session.active_dataset == "customers.csv"
    assert session.df is session.tables["customers"]


def test_table_keys_are_safe_identifiers(session: Session) -> None:
    session.add_dataset("Q3 sales (final).csv", pd.DataFrame({"a": [1]}))
    assert "q3_sales_final" in session.tables


def test_every_table_is_materialised_for_the_sandbox(session: Session) -> None:
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1]}))
    session.add_dataset("customers.csv", pd.DataFrame({"id": [1]}))

    tables_dir = session.workspace / "tables"
    assert (tables_dir / "orders.feather").exists()
    assert (tables_dir / "customers.feather").exists()
    # The active table is still bound to `df` the way every prompt assumes.
    assert (session.workspace / "dataset.feather").exists()


def test_removing_a_table_removes_its_materialised_copy(session: Session) -> None:
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1]}))
    session.add_dataset("customers.csv", pd.DataFrame({"id": [1]}))

    assert session.remove_dataset("orders.csv")
    assert not (session.workspace / "tables" / "orders.feather").exists()
    assert "orders" not in session.tables


async def test_generated_code_can_join_across_tables(session: Session, stub_llm) -> None:  # noqa: F811
    """End to end, on the Docker-less path: `tables` is in the namespace."""
    session.add_dataset("orders.csv", pd.DataFrame({"customer_id": [10, 11], "amount": [5, 7]}))
    session.add_dataset("customers.csv", pd.DataFrame({"customer_id": [10, 11], "name": ["ann", "bo"]}))

    stub_llm(
        [
            "1. Join them",
            (
                "```python\n"
                + "joined = tables['orders'].merge(tables['customers'], on='customer_id')\n"
                + "print(joined['amount'].sum())\n"
                + "```"
            ),
            "The total is 12.",
        ]
    )

    result = await orchestrator.run(
        session=session, instruction="total by customer", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert "12" in result.answer


def test_inspect_reports_the_other_tables(session: Session) -> None:
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1, 2]}))
    session.add_dataset("customers.csv", pd.DataFrame({"id": [1]}))

    summary = session.inspect("what is here")

    assert "tables[" in summary
    assert "orders" in summary


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("check the nulls", "missing"),
        ("tell me about column A", "distinct"),
        ("", "|"),
    ],
)
def test_inspection_detail_follows_the_goal(loaded_session: Session, goal: str, expected: str) -> None:
    """`inspect` is only worth an iteration if it answers the question asked."""
    assert expected in loaded_session.inspect(goal).lower() or expected in loaded_session.inspect(goal)


async def test_a_used_skill_is_recorded_for_the_browser(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """The `skill` frame is live and gone once the turn ends; the milestone's
    browser has to answer "which analyses used this" later."""
    from src.core.database import db_mgr

    stub_llm(["1. Go", "```python\nprint(1)\n```", "```python\npass\n```", "Done."])

    await orchestrator.run(
        session=loaded_session, instruction="cohort retention by signup date", mode="fast", emitter=EventCollector()
    )

    assert db_mgr.skill_usage_summary()["cohort-method"]["uses"] == 1
    assert db_mgr.get_skill_usage("cohort-method")[0]["instruction"] == "cohort retention by signup date"


async def test_usage_is_recorded_even_when_the_turn_fails(
    loaded_session: Session,  # noqa: F811
    stub_llm,
    installed_skill,
) -> None:
    """A skill informed the plan whether or not the code that followed worked.

    Counting only the wins would misreport the skill that is reached for and
    keeps failing, which is exactly the one worth finding.
    """
    from src.core.database import db_mgr

    stub_llm(["1. Go", "```python\nimport os\n```", "```python\nimport os\n```", "Done."])

    await orchestrator.run(
        session=loaded_session, instruction="cohort retention by signup date", mode="fast", emitter=EventCollector()
    )

    assert db_mgr.skill_usage_summary().get("cohort-method", {}).get("uses") == 1


async def test_repeating_the_same_analysis_reuses_every_phase_14_cache(
    loaded_session: Session,
    stub_llm,
    monkeypatch,  # noqa: F811
) -> None:
    """Phase 14: a second turn against the exact same code and the exact same dataset skips the
    verification model call, the data-understanding recomputation, and the validator re-run --
    all three are pure functions of inputs that have not changed, so reusing the first turn's
    results is exact, never an approximation. This is the benchmark harness's own `score_cost`
    metric (model calls per turn) proved directly: the second turn needs one fewer LLM call for
    the identical shape of turn.
    """
    import src.core.agent.orchestrator as orchestrator_module
    from src.core.analysis import understanding as understanding_module

    understand_calls = 0
    real_understand = understanding_module.understand

    def counting_understand(*args, **kwargs):
        nonlocal understand_calls
        understand_calls += 1
        return real_understand(*args, **kwargs)

    monkeypatch.setattr(understanding_module, "understand", counting_understand)

    validator_calls = 0
    real_run_validators = orchestrator_module.run_validators

    def counting_run_validators(*args, **kwargs):
        nonlocal validator_calls
        validator_calls += 1
        return real_run_validators(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "run_validators", counting_run_validators)

    full_turn = [
        "1. Compute",
        "```python\nprint(df['A'].sum())\n```",
        "ACTION: answer\nGOAL: report",
        "```python\nprint('VERIFIED: ok')\n```",
        "The total is 15.",
    ]
    stub_llm(list(full_turn))
    result1 = await orchestrator.run(
        session=loaded_session, instruction="total of A", mode="auto", emitter=EventCollector()
    )

    # A differently-worded instruction, so the (unrelated) semantic cache's exact-match lookup
    # does not itself short-circuit this turn before Phase 14's caches are ever reached -- the
    # code produced is still byte-identical, which is what every cache here actually keys on.
    # No verify-code response this time -- a cache hit must mean it is never asked for.
    second_turn_stub = stub_llm(
        ["1. Compute", "```python\nprint(df['A'].sum())\n```", "ACTION: answer\nGOAL: report", "The total is 15."]
    )
    result2 = await orchestrator.run(
        session=loaded_session, instruction="recompute the total of A", mode="auto", emitter=EventCollector()
    )

    assert result1.status == "completed"
    assert result2.status == "completed"
    assert len(second_turn_stub.prompts) == 4, "the verification model call should have been skipped"
    assert result2.verification == result1.verification
    assert understand_calls == 1, "the second turn should reuse the cached understanding"
    assert validator_calls == 1, "the second turn should reuse the cached validator findings"
