from __future__ import annotations

from src.core.prompts import (
    MAX_ANSWER_CODE_CHARS,
    MAX_ANSWER_FINDINGS_CHARS,
    MAX_ANSWER_OUTPUT_CHARS,
    create_answer_prompt,
    create_decision_prompt,
    prompt_chars,
)


def test_answer_prompt_caps_long_code_output_and_findings() -> None:
    prompt = create_answer_prompt(
        "summarise",
        "x = 1\n" * 20_000,
        "stdout\n" * 20_000,
        findings=["finding\n" * 1000 for _ in range(20)],
    )
    assert prompt_chars(prompt) < MAX_ANSWER_CODE_CHARS + MAX_ANSWER_OUTPUT_CHARS + MAX_ANSWER_FINDINGS_CHARS + 10_000
    assert "... [" in prompt


def test_decision_prompt_keeps_recent_investigation_history() -> None:
    prompt = create_decision_prompt(
        "question",
        "plan",
        "old\n" * 20_000 + "RECENT-EVIDENCE",
        iteration=1,
        remaining=2,
        allowed=["code", "answer"],
    )
    assert prompt_chars(prompt) <= 20_000
    assert "RECENT-EVIDENCE" in prompt
