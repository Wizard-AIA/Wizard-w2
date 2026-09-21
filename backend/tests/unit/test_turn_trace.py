from src.core.agent.trace import TurnTrace


def test_turn_trace_is_structured_and_contains_no_message_text() -> None:
    trace = TurnTrace(
        turn_id="turn-1",
        intent="computation",
        workflow="direct",
        complexity="simple",
        planner_used=False,
        models_used=["local-model"],
        tools_actions_invoked=["code"],
        generation_budgets={"code": 4096},
        context_chars=123,
        output_tokens=17,
        llm_call_count=1,
        latency_ms=42,
        termination_reason="completed",
    )
    record = trace.to_log()
    assert record == trace.to_dict()
    assert record["turn_id"] == "turn-1"
    assert record["generation_budgets"] == {"code": 4096}
    assert "message" not in record
    assert "prompt" not in record
