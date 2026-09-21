from __future__ import annotations

import pytest

from src.config import settings
from src.core.llm.adapters import AnthropicAdapter, OllamaAdapter, OpenAICompatibleAdapter, adapter_for
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


def test_fast_mode_never_shrinks_a_program_or_a_plan() -> None:
    """Fast saves calls, not tokens per call: a program cut off mid-statement is a failed call."""
    for purpose in ("code", "plan", "decision", "answer", "review"):
        fast = resolve_generation(purpose, mode="fast")
        auto = resolve_generation(purpose, mode="auto")
        assert fast.config.max_output_tokens == auto.config.max_output_tokens, purpose


def test_deep_mode_widens_only_the_calls_that_can_use_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_TOKENS", 32_000)
    for purpose in ("plan", "code", "answer"):
        deep = resolve_generation(purpose, mode="deep").config.max_output_tokens
        auto = resolve_generation(purpose, mode="auto").config.max_output_tokens
        assert deep == int(auto * 1.5), purpose
    for purpose in ("decision", "review", "converse"):
        assert (
            resolve_generation(purpose, mode="deep").config.max_output_tokens
            == resolve_generation(purpose, mode="auto").config.max_output_tokens
        ), purpose


def test_deep_never_exceeds_the_process_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS_CODE", 4096)
    resolved = resolve_generation("code", mode="deep")
    assert resolved.config.max_output_tokens == 4096
    assert resolved.provenance["max_output_tokens"] == "system-safe clamp"


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gpt-4o-mini", "phi-3-mini", "tiny-llama", "qwen3-nano"])
def test_a_budget_is_not_guessed_from_a_model_name(model: str) -> None:
    """ "mini" is inside "gemini". A name is not evidence of an output ceiling."""
    with_model = resolve_generation("code", model=model, provider="openai")
    without = resolve_generation("code")
    assert with_model.config.max_output_tokens == without.config.max_output_tokens


def test_provider_adapters_use_the_names_the_clients_accept() -> None:
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

    # Spellings carried over unchanged from before v1.0.14; see the adapter docstring.
    anthropic = AnthropicAdapter().translate(config)
    assert anthropic.values["max_tokens_to_sample"] == 123
    assert anthropic.values["stop"] == ["END"]
    assert anthropic.values["timeout"] == 3
    assert anthropic.values["top_k"] == 12

    compatible = OpenAICompatibleAdapter("gpt-4o").translate(config)
    assert compatible.values["max_tokens"] == 123
    assert "max_completion_tokens" not in compatible.values
    assert set(compatible.dropped) == {"top_k", "num_ctx"}


@pytest.mark.parametrize("model", ["gpt-4o", "o3-mini", "gpt-5", "qwen3-thinking", ""])
def test_openai_compatible_always_sends_max_tokens(model: str) -> None:
    """The client maps it to max_completion_tokens where needed; a name heuristic would only disagree."""
    values = OpenAICompatibleAdapter(model).translate(GenerationConfig(max_output_tokens=200)).values
    assert values["max_tokens"] == 200
    assert "max_completion_tokens" not in values


@pytest.mark.parametrize(
    ("provider", "api_style", "expected"),
    [
        ("ollama", "", OllamaAdapter),
        ("anthropic", "", AnthropicAdapter),
        ("openai", "", OpenAICompatibleAdapter),
        ("gemini", "", OpenAICompatibleAdapter),
        ("lmstudio", "", OpenAICompatibleAdapter),
        ("", "anthropic", AnthropicAdapter),
    ],
)
def test_adapter_selection(provider: str, api_style: str, expected: type) -> None:
    assert type(adapter_for(provider, api_style)) is expected
