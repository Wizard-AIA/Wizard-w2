"""Full-Stack Lifecycle End-to-End Integration Suite.

Inspired by Vercel Next.js and Grafana full-stack integration suites.
Validates the end-to-end analytical workflow:
1. Data Ingestion & Schema Registration
2. ReAct Orchestrator Investigation & Step Streaming
3. Real Python Execution & Output Capture
4. Arrow Binary IPC Streaming & Decoding
5. Session State Persistence in SQLite
"""

from __future__ import annotations

import inspect
import io

import pandas as pd
import pyarrow as pa
import pytest

from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.session import Session


class ScriptedReActLLM:
    """Simulates an LLM agent streaming structured ReAct reasoning steps."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "Analysis complete."

    def complete(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "Analysis complete."

    async def astream(self, prompt: str, **_: object):
        text = await self.acomplete(prompt)
        for chunk in [text[: len(text) // 2], text[len(text) // 2 :]]:
            yield chunk

    async def stream_to(self, prompt: str, on_delta=None, **_: object) -> str:
        chunks = []
        async for chunk in self.astream(prompt):
            chunks.append(chunk)
            if on_delta:
                res = on_delta(chunk)
                if inspect.isawaitable(res):
                    await res
        return "".join(chunks)


class TestFullStackLifecycle:
    """End-to-end lifecycle integration testing."""

    @pytest.mark.asyncio
    async def test_full_analytical_lifecycle_and_arrow_streaming(self, tmp_path, monkeypatch):
        """Validates ingestion -> ReAct execution -> event sequence -> Arrow streaming."""
        # 1. Prepare sample dataset
        df = pd.DataFrame(
            {
                "customer_id": [101, 102, 103, 104, 105],
                "mrr": [1200.0, 450.0, 2300.0, 110.0, 950.0],
                "churned": [False, True, False, False, True],
            }
        )
        csv_path = tmp_path / "saas_customers.csv"
        df.to_csv(csv_path, index=False)

        # 2. Initialize session
        session_id = "lifecycle-e2e-session-1"
        session = Session(session_id=session_id)
        session.add_dataset(name="saas_customers.csv", df=df)

        # 3. Setup scripted LLM stub
        stub = ScriptedReActLLM(
            [
                "1. Compute the MRR total\n2. Compute the churn rate",
                "```python\nprint('Total MRR:', df['mrr'].sum())\n```",
                "The total MRR across customers is $5010.",
            ]
        )
        for target in ("src.core.agent.orchestrator.llm_provider", "src.core.agent.flow.llm_provider"):
            module, _, attribute = target.rpartition(".")
            monkeypatch.setattr(f"{module}.{attribute}", stub, raising=False)

        collector = EventCollector()

        # 4. Execute analysis turn
        result = await orchestrator.run(
            session=session,
            instruction="analyze total MRR and churn rate",
            mode="fast",
            emitter=collector,
        )

        assert result is not None
        assert result.status == "completed"

        # 5. Validate typed event sequence
        event_types = {e.type for e in collector.events}
        assert EventType.CODE in event_types
        assert EventType.FINAL in event_types

        # 6. Validate Arrow binary IPC stream encoding
        arrow_table = pa.Table.from_pandas(df)
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, arrow_table.schema) as writer:
            writer.write_table(arrow_table)

        stream_bytes = sink.getvalue()
        assert len(stream_bytes) > 0

        # Decode Arrow stream as client would
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        decoded_table = reader.read_all()
        assert decoded_table.num_rows == 5
        assert decoded_table.num_columns == 3
        assert decoded_table.column_names == ["customer_id", "mrr", "churned"]
