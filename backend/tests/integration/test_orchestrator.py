"""Integration tests for the analysis workflow.

The LLM is replaced with a scripted stub so the full orchestrator — routing,
planning, generation, guarding, execution, correction, answer synthesis and
persistence — runs deterministically without a model server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pandas as pd
import pytest

from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import AnalysisOrchestrator, orchestrator
from src.core.database import db_mgr
from src.core.llm.provider import LLMUnavailableError
from src.core.semantic_cache import semantic_cache
from src.core.session import Session


class ScriptedLLM:
    """Returns queued responses in order and records every prompt it received."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def _next(self) -> str:
        self.prompts.append("")
        return self.responses.pop(0) if self.responses else "No more scripted responses."

    async def acomplete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "Done."

    def complete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "Done."

    async def astream(self, prompt: str, **_: object) -> AsyncIterator[str]:
        text = await self.acomplete(prompt)
        # Emit in small pieces so streaming behaviour is genuinely exercised.
        for index in range(0, len(text), 7):
            yield text[index : index + 7]

    async def stream_to(self, prompt: str, on_delta=None, **_: object) -> str:
        chunks: list[str] = []
        async for delta in self.astream(prompt):
            chunks.append(delta)
            if on_delta is not None:
                result = on_delta(delta)
                if hasattr(result, "__await__"):
                    _ = await result
        return "".join(chunks)


@pytest.fixture
def stub_llm(monkeypatch):
    """Installs a scripted LLM into every module that reached for the provider."""

    def install(responses: list[str]) -> ScriptedLLM:
        stub = ScriptedLLM(responses)
        for target in ("src.core.agent.orchestrator.llm_provider", "src.core.agent.flow.llm_provider"):
            module, _, attribute = target.rpartition(".")
            monkeypatch.setattr(f"{module}.{attribute}", stub, raising=False)
        return stub

    return install


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "instruction",
    ["show first 5 rows", "show columns", "how many rows are there", "preview dataset"],
)
def test_simple_requests_bypass_planning(instruction: str) -> None:
    assert AnalysisOrchestrator.is_simple(instruction)


@pytest.mark.parametrize(
    "instruction",
    ["build a regression model", "plot revenue against spend", "find and explain outliers"],
)
def test_complex_requests_do_not_bypass_planning(instruction: str) -> None:
    assert not AnalysisOrchestrator.is_simple(instruction)


def test_visual_revision_detection() -> None:
    plotting_code = "import matplotlib.pyplot as plt\nplt.plot(df['A'])"
    assert AnalysisOrchestrator.is_visual_revision("make the colour red", plotting_code)
    assert not AnalysisOrchestrator.is_visual_revision("compute the mean", plotting_code)
    assert not AnalysisOrchestrator.is_visual_revision("make the colour red", "print(df.head())")


@pytest.mark.parametrize(
    "raw,mode",
    [
        ("auto", "auto"),
        ("fast", "fast"),
        ("deep", "deep"),
        ("planning", "planning"),  # legacy alias, still honoured
        ("PLANNING", "planning"),
        ("nonsense", "auto"),
        ("", "auto"),
        (None, "auto"),
    ],
)
def test_mode_normalisation(raw: str | None, mode: str) -> None:
    """An unknown mode must never reach the loop as a budget lookup miss."""
    assert AnalysisOrchestrator.normalise_mode(raw) == mode


@pytest.mark.parametrize(
    "response,expected",
    [
        ("```python\nprint(1)\n```", "print(1)"),
        ("```py\nprint(2)\n```", "print(2)"),
        ("Some prose\n```python\nprint(3)\n```\nmore prose", "print(3)"),
        ("print(4)", "print(4)"),
    ],
)
def test_code_extraction(response: str, expected: str) -> None:
    assert AnalysisOrchestrator._extract_code(response) == expected


# --------------------------------------------------------------------------- #
# Full runs
# --------------------------------------------------------------------------- #
async def test_fast_mode_runs_end_to_end(loaded_session: Session, stub_llm) -> None:
    # A single numbered line stays a one-shot run; two or more become a
    # step-by-step notebook execution (covered separately below).
    stub_llm(
        [
            "1. Print the row count",
            "```python\nprint('rows:', len(df))\n```",
            "The dataset contains 5 rows.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="how big is this data", mode="fast", emitter=collector
    )

    assert result.status == "completed"
    assert "5 rows" in result.answer
    assert "print" in result.code

    types = {event.type for event in collector.events}
    assert EventType.CODE in types
    assert EventType.CONTENT_DELTA in types
    assert EventType.FINAL in types


async def test_run_carries_and_persists_analytical_state(loaded_session: Session, stub_llm) -> None:
    """Phase 1: `analysis` rides alongside the existing result fields, and its
    findings/assumptions agree with the Investigation-derived ones -- the loop's
    observable behaviour is unchanged, it just now also carries structured state.
    """
    stub_llm(
        [
            "1. Print the row count",
            "```python\nprint('rows:', len(df))\n```",
            "The dataset contains 5 rows.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="how big is this data", mode="fast", emitter=collector
    )

    assert isinstance(result.analysis, dict)
    assert result.analysis["findings"] == result.findings
    assert result.analysis["assumptions"] == result.assumptions
    assert result.analysis["objective"] == {  # populated deterministically since Phase 8
        "question": "how big is this data",
        "analytical_type": None,  # no keyword in `_TYPE_KEYWORDS` matches this phrasing
        "unit_of_analysis": None,
        "population": None,
        "time_dimension": None,
        "likely_variables": {},
        "constraints": [],
        "expected_output": None,
        "ambiguity": [],
    }

    final = collector.of_type(EventType.FINAL)[0]
    assert final.data["analysis"] == result.analysis

    assert result.message_id is not None
    stored = db_mgr.get_analysis_state(result.message_id)
    assert stored == result.analysis


async def test_answer_is_streamed_in_multiple_deltas(loaded_session: Session, stub_llm) -> None:
    """The point of the rewrite: tokens reach the client as they are produced."""
    stub_llm(
        [
            "1. Compute\n2. Report",
            "```python\nprint(df['A'].mean())\n```",
            "The mean of column A is 3.0, which sits at the centre of the range.",
        ]
    )
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="mean of A", mode="fast", emitter=collector)

    deltas = collector.of_type(EventType.CONTENT_DELTA)
    assert len(deltas) > 1, "the answer arrived in a single chunk, so nothing streamed"
    assert collector.text_of(EventType.CONTENT_DELTA).startswith("The mean")


async def test_planning_mode_halts_for_approval(loaded_session: Session, stub_llm) -> None:
    stub_llm(["<thought>Considering the request.</thought>\n1. Load data\n2. Summarise it"])
    collector = EventCollector()

    result = await orchestrator.run(
        session=loaded_session, instruction="summarise the dataset", mode="planning", emitter=collector
    )

    assert result.status == "awaiting_approval"
    assert result.pending_approval["tool"] == "execute_plan"
    assert result.thought == "Considering the request."
    assert not result.code, "code must not be generated before the plan is approved"
    assert collector.of_type(EventType.APPROVAL_REQUIRED)


async def test_reasoning_and_plan_are_streamed_separately(loaded_session: Session, stub_llm) -> None:
    stub_llm(["<thought>Private reasoning here.</thought>\n1. Step one\n2. Step two"])
    collector = EventCollector()

    await orchestrator.run(session=loaded_session, instruction="analyse this", mode="planning", emitter=collector)

    reasoning = collector.text_of(EventType.REASONING_DELTA)
    plan = collector.text_of(EventType.PLAN_DELTA)

    assert "Private reasoning" in reasoning
    assert "<thought>" not in reasoning and "<thought>" not in plan
    assert "Step one" in plan


async def test_approved_plan_skips_replanning(loaded_session: Session, stub_llm) -> None:
    stub = stub_llm(["```python\nprint('ok')\n```", "Executed successfully."])

    result = await orchestrator.run(
        session=loaded_session,
        instruction="run it",
        mode="fast",
        approved_plan="1. Print ok\n2. Stop",
        emitter=EventCollector(),
    )

    assert result.status == "completed"
    assert result.plan == "1. Print ok\n2. Stop", "the approved plan is used verbatim"
    # Only code generation and answer synthesis: no second planning round-trip.
    assert len(stub.prompts) == 2, "an approved plan must not trigger another planning call"


async def test_search_request_halts_for_consent(loaded_session: Session, stub_llm) -> None:
    stub_llm(['<thought>I need context.</thought>\nSEARCH: "current inflation rate"'])

    result = await orchestrator.run(
        session=loaded_session, instruction="adjust for inflation", mode="fast", emitter=EventCollector()
    )

    assert result.status == "awaiting_approval"
    assert result.pending_approval["tool"] == "web_search"
    assert result.pending_approval["query"] == "current inflation rate"


async def test_local_only_refuses_a_search_rather_than_asking(loaded_session: Session, stub_llm) -> None:
    """Under local-only there is no consent that would make this allowed, so
    prompting for one would be theatre. The run continues without the search and
    says why, rather than stopping."""
    loaded_session.data_mode = "local-only"
    stub_llm(
        [
            '<thought>I need context.</thought>\nSEARCH: "current inflation rate"',
            "```python\nprint('done')\n```",
            "All done.",
        ]
    )

    result = await orchestrator.run(
        session=loaded_session, instruction="adjust for inflation", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert result.pending_approval is None
    assert any("local-only" in warning for warning in result.warnings)


async def test_a_turn_reports_what_it_cost(loaded_session: Session, stub_llm) -> None:
    """The readout has to survive to the client; the ledger being right is not
    enough if nothing carries it out of the orchestrator."""
    stub_llm(["1. Print ok\n2. Stop", "```python\nprint('ok')\n```", "It printed ok."])

    result = await orchestrator.run(
        session=loaded_session, instruction="print ok", mode="fast", emitter=EventCollector()
    )

    assert "usage" in result.to_dict()
    assert result.usage["calls"] >= 0


# --------------------------------------------------------------------------- #
# Guarding and correction
# --------------------------------------------------------------------------- #
async def test_unsafe_code_is_blocked_and_reported(loaded_session: Session, stub_llm) -> None:
    stub_llm(["1. Do it\n2. Finish", "```python\nimport os\nos.system('id')\n```"])

    result = await orchestrator.run(
        session=loaded_session, instruction="list the files", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"
    assert "safety guard" in result.answer.lower()
    assert "os" in result.answer


async def test_runtime_failure_triggers_self_correction(loaded_session: Session, stub_llm) -> None:
    stub_llm(
        [
            "1. Read a column\n2. Print it",
            "```python\nprint(df['MISSING'])\n```",  # raises KeyError
            "```python\nprint(df['A'].sum())\n```",  # the fix
            "Column A sums to 15.",
        ]
    )
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="total of A", mode="fast", emitter=collector)

    assert result.status == "completed"
    assert "15" in result.answer
    # The second attempt must have been prompted with the traceback.
    assert any(event.data.get("phase") == "correcting" for event in collector.of_type(EventType.STATUS))


async def test_successful_heal_records_a_trajectory(loaded_session: Session, stub_llm) -> None:
    """Regression: `state.error` was never cleared after a successful retry, so
    the `if code and not error` guard in the review step was always False and
    neither the cache nor the trajectory memory was ever written."""
    stub_llm(
        [
            "1. Compute\n2. Print",
            "```python\nprint(df['NOPE'])\n```",
            "```python\nprint(df['A'].max())\n```",
            "The maximum is 5.",
        ]
    )

    before = len(db_mgr.get_trajectory_entries(["A", "B", "C"]))
    await orchestrator.run(session=loaded_session, instruction="max of A", mode="fast", emitter=EventCollector())
    after = len(db_mgr.get_trajectory_entries(["A", "B", "C"]))

    assert after > before, "a failure-then-fix trajectory should have been recorded"


async def test_successful_run_is_cached(loaded_session: Session, stub_llm) -> None:
    stub_llm(["1. Compute\n2. Print", "```python\nprint(df['A'].min())\n```", "The minimum is 1."])

    await orchestrator.run(
        session=loaded_session, instruction="minimum of column A", mode="fast", emitter=EventCollector()
    )

    cached = semantic_cache.lookup("minimum of column A", ["A", "B", "C"])
    assert cached is not None
    assert "min" in cached


async def test_retries_are_bounded(loaded_session: Session, stub_llm) -> None:
    failing = "```python\nprint(df['ALWAYS_MISSING'])\n```"
    stub_llm(["1. Try\n2. Again", failing, failing, failing, failing, failing, "It failed."])

    result = await orchestrator.run(
        session=loaded_session, instruction="do the impossible", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"  # reported, not hung


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
async def test_missing_dataset_is_reported(session: Session, stub_llm) -> None:
    stub_llm([])
    collector = EventCollector()

    result = await orchestrator.run(session=session, instruction="analyse", mode="fast", emitter=collector)

    assert result.status == "failed"
    assert collector.of_type(EventType.ERROR)


async def test_unreachable_model_produces_a_clear_message(loaded_session: Session, monkeypatch) -> None:
    class DeadLLM:
        async def stream_to(self, *args, **kwargs):
            raise LLMUnavailableError("connection refused")

        async def acomplete(self, *args, **kwargs):
            raise LLMUnavailableError("connection refused")

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", DeadLLM())
    collector = EventCollector()

    result = await orchestrator.run(session=loaded_session, instruction="analyse", mode="fast", emitter=collector)

    assert result.status == "failed"
    assert "Ollama" in result.answer or "language model" in result.answer


async def test_history_is_persisted_for_the_session(loaded_session: Session, stub_llm) -> None:
    stub_llm(["1. Compute\n2. Print", "```python\nprint(1)\n```", "The answer is 1."])

    await orchestrator.run(session=loaded_session, instruction="what is one", mode="fast", emitter=EventCollector())

    history = loaded_session.history()
    assert any(message["role"] == "assistant" for message in history)


async def test_multi_step_plans_execute_each_step(loaded_session: Session, stub_llm) -> None:
    stub_llm(
        [
            "1. Compute the sum of A\n2. Compute the mean of C\n3. Report both",
            "```python\nprint('sum', df['A'].sum())\n```",
            "```python\nprint('mean', df['C'].mean())\n```",
            "```python\nprint('done')\n```",
            "Both statistics were computed.",
        ]
    )

    result = await orchestrator.run(
        session=loaded_session, instruction="summarise A and C", mode="fast", emitter=EventCollector()
    )

    assert result.status == "completed"


def test_downloads_exclude_the_source_dataset(loaded_session: Session) -> None:
    (loaded_session.workspace / "output.csv").write_text("a\n1\n", encoding="utf-8")

    from src.core.agent.orchestrator import RunState

    downloads = AnalysisOrchestrator._collect_downloads(RunState(instruction="x"), loaded_session)

    assert "output.csv" in downloads
    assert "dataset.csv" not in downloads
    assert "dataset.feather" not in downloads


def test_session_dataset_is_materialised_for_the_sandbox(session: Session, simple_df: pd.DataFrame) -> None:
    session.add_dataset("dataset.csv", simple_df)
    assert (session.workspace / "dataset.csv").exists()
    assert (session.workspace / "dataset.feather").exists()


# --------------------------------------------------------------------------- #
# Per-role provider routing
# --------------------------------------------------------------------------- #
class RecordingLLM(ScriptedLLM):
    """Scripted, but also records which (role, model, provider) each call used."""

    def __init__(self, responses: list[str]):
        super().__init__(responses)
        self.calls: list[dict] = []

    def _record(self, kwargs: dict) -> None:
        self.calls.append(
            {
                "role": str(kwargs.get("role", "")),
                "model": kwargs.get("model"),
                "provider": kwargs.get("provider"),
            }
        )

    async def acomplete(self, prompt: str, **kwargs: object) -> str:
        self._record(kwargs)
        return await super().acomplete(prompt)

    def complete(self, prompt: str, **kwargs: object) -> str:
        self._record(kwargs)
        return super().complete(prompt)

    async def stream_to(self, prompt: str, on_delta=None, **kwargs: object) -> str:
        self._record(kwargs)
        return await super().stream_to(prompt, on_delta)


async def test_each_role_reaches_its_own_provider(loaded_session: Session, monkeypatch) -> None:
    """The point of per-session providers: plan on one backend, code on another."""
    stub = RecordingLLM(
        [
            "<thought>Considering.</thought>\n1. Count the rows",
            "```python\nprint(len(df))\n```",
            "There are five rows.",
        ]
    )
    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", stub)

    loaded_session.models.manager = "deepseek-r1:1.5b"
    loaded_session.models.manager_provider = "ollama"
    loaded_session.models.worker = "qwen2.5-coder-7b-instruct"
    loaded_session.models.worker_provider = "lmstudio"

    await orchestrator.run(session=loaded_session, instruction="how many rows", mode="fast", emitter=EventCollector())

    by_role: dict[str, dict] = {}
    for call in stub.calls:
        by_role.setdefault(call["role"], call)

    assert by_role["manager"]["provider"] == "ollama"
    assert by_role["manager"]["model"] == "deepseek-r1:1.5b"
    assert by_role["worker"]["provider"] == "lmstudio"
    assert by_role["worker"]["model"] == "qwen2.5-coder-7b-instruct"


async def test_unset_provider_leaves_the_default_in_charge(loaded_session: Session, monkeypatch) -> None:
    """A session that never touches the provider fields behaves as it always did."""
    stub = RecordingLLM(["1. Count", "```python\nprint(len(df))\n```", "Five."])
    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", stub)

    await orchestrator.run(session=loaded_session, instruction="how many rows", mode="fast", emitter=EventCollector())

    assert stub.calls
    assert all(call["provider"] is None for call in stub.calls)
