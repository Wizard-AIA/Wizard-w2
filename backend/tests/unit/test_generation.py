from __future__ import annotations

import pytest

from src.config import settings
from src.core.llm.adapters import AnthropicAdapter, GeminiAdapter, OllamaAdapter, OpenAICompatibleAdapter
from src.core.llm.generation import GenerationConfig, resolve_generation


def test_precedence_and_safe_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_TOKENS", 900)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS_ANSWER", 400)

    resolved = resolve_generation(
        "answer",
        mode="deep",
        workflow="agentic",
        provider="openai",
        model="gpt-4o",
        override={"max_output_tokens": 50_000, "top_p": 0.8},
    )

    assert resolved.config.max_output_tokens == 900
    assert resolved.provenance["max_output_tokens"] == "system-safe clamp"
    assert resolved.config.top_p == 0.8
    assert resolved.provenance["top_p"] == "request override"


def test_mode_and_workflow_budgets_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_TOKENS", 8192)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS_ANSWER", 4096)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS_CONVERSE", 512)

    direct = resolve_generation("answer", workflow="direct")
    agentic = resolve_generation("answer", workflow="agentic")
    converse = resolve_generation("converse", workflow="converse")

    assert direct.config.max_output_tokens == 1536
    assert agentic.config.max_output_tokens == 4096
    assert converse.config.max_output_tokens == 512


def test_explain_and_dict_are_text_free_of_prompt_content() -> None:
    resolved = resolve_generation("decision", provider="ollama", model="qwen")
    assert "decision uses" in resolved.explain()
    assert resolved.to_dict()["config"]["max_output_tokens"] == resolved.config.max_output_tokens


def test_provider_adapters_use_current_wire_parameter_names() -> None:
    config = GenerationConfig(
        max_output_tokens=123,
        temperature=0.2,
        top_p=0.9,
        top_k=12,
        stop=("END",),
        num_ctx=4096,
        timeout_s=3,
    )

    ollama = OllamaAdapter().translate(config)
    assert ollama.values["num_predict"] == 123
    assert ollama.values["client_kwargs"] == {"timeout": 3}
    assert ollama.values["num_ctx"] == 4096

    anthropic = AnthropicAdapter().translate(config)
    assert anthropic.values["max_tokens"] == 123
    assert "max_tokens_to_sample" not in anthropic.values

    standard = OpenAICompatibleAdapter("gpt-4o").translate(config)
    reasoning = OpenAICompatibleAdapter("o3-mini").translate(config)
    assert standard.values["max_tokens"] == 123
    assert reasoning.values["max_completion_tokens"] == 123
    assert "top_k" in standard.dropped
    assert "num_ctx" in standard.dropped

    gemini = GeminiAdapter().translate(config)
    assert gemini.values["max_output_tokens"] == 123
    assert gemini.values["stop_sequences"] == ["END"]
    assert "num_ctx" in gemini.dropped
