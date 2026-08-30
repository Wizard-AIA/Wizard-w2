import asyncio

import pytest

from src.core.context_summarizer import ContextSummarizer
from src.core.database import db_mgr
from src.core.infra.metrics import MetricsCollector
from src.core.infra.session_bus import SessionEventBus
from src.core.infra.telemetry import extract_trace_context, get_tracer, inject_trace_context


def test_telemetry_fallback():
    tracer = get_tracer("test.tracer")
    with tracer.start_as_current_span("test_span"):
        carrier = {}
        inject_trace_context(carrier)
        assert isinstance(carrier, dict)
        extracted = extract_trace_context(carrier)
        assert extracted is None or isinstance(extracted, dict)


def test_metrics_collector_and_prometheus_export():
    collector = MetricsCollector()
    collector.record_request("GET", "/health", 200, 0.015)
    collector.record_ttft("ollama", "qwen2.5", 0.45)
    collector.record_error("timeout")

    text = collector.generate_prometheus_text()
    assert "wizard_request_duration_seconds" in text
    assert "wizard_llm_ttft_seconds" in text
    assert "wizard_errors_total" in text
    assert 'method="GET"' in text


@pytest.mark.asyncio
async def test_session_event_bus_inprocess():
    bus = SessionEventBus()
    received = []

    async def listener():
        async for evt in bus.subscribe("sess-123"):
            received.append(evt)
            break

    task = asyncio.create_task(listener())
    await asyncio.sleep(0.01)
    bus.publish("sess-123", {"event": "test_ping"})
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0]["event"] == "test_ping"


def test_context_summarizer_deterministic():
    summarizer = ContextSummarizer()
    history = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "response 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "response 2"},
        {"role": "user", "content": "load sales_data.csv and calculate total revenue"},
        {"role": "assistant", "content": "Filtered for 2026. Total revenue is $1,250,000 across 4,500 rows."},
    ]
    summary = summarizer.summarize(history, turn_count_threshold=2)
    assert isinstance(summary, dict)
    assert "summary" in summary
    assert len(summary["summary"]) > 0


def test_database_prune_old_analysis_data():
    deleted = db_mgr.prune_old_analysis_data(days=365)
    assert isinstance(deleted, dict)
    assert "analysis_state" in deleted
    assert "analysis_runs" in deleted
