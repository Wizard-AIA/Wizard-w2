"""Provider-family translation for :mod:`generation`.

Adapters return constructor/request kwargs and an explicit record of fields
that were dropped because the provider cannot represent them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .generation import GenerationConfig


@dataclass(frozen=True)
class AdapterPayload:
    values: dict[str, Any]
    dropped: dict[str, str]


class GenerationAdapter:
    supported: frozenset[str] = frozenset()
    names: dict[str, str] = {}

    def __init__(self, model: str = "") -> None:
        self.model = model

    def translate(self, config: GenerationConfig) -> AdapterPayload:
        values: dict[str, Any] = {}
        dropped: dict[str, str] = {}
        for field, value in config.to_dict().items():
            if value is None or field == "max_output_tokens":
                continue
            if field in self.supported:
                values[self.names.get(field, field)] = value
            else:
                dropped[field] = "unsupported by provider"
        values[self.names.get("max_output_tokens", "max_output_tokens")] = config.max_output_tokens
        return AdapterPayload(values=values, dropped=dropped)


class OllamaAdapter(GenerationAdapter):
    supported = frozenset({"temperature", "top_p", "top_k", "stop", "num_ctx", "timeout_s"})
    names = {"max_output_tokens": "num_predict", "timeout_s": "client_kwargs"}

    def translate(self, config: GenerationConfig) -> AdapterPayload:
        result = super().translate(config)
        values = dict(result.values)
        if "client_kwargs" in values:
            values["client_kwargs"] = {"timeout": values["client_kwargs"]}
        return AdapterPayload(values, result.dropped)


class AnthropicAdapter(GenerationAdapter):
    """Spellings this codebase already passed to ``ChatAnthropic`` before v1.0.14.

    Verified against ``langchain-anthropic 1.7``: ``max_tokens_to_sample`` is the
    alias of ``ChatAnthropic.max_tokens``. The package is optional, so the test
    that builds the real client skips where it is absent. If the client renames
    a field, the provider call fails loudly at construction; it does not
    silently drop the budget.
    """

    supported = frozenset({"temperature", "top_p", "top_k", "stop", "timeout_s"})
    names = {
        "max_output_tokens": "max_tokens_to_sample",
        "timeout_s": "timeout",
    }


class OpenAICompatibleAdapter(GenerationAdapter):
    """OpenAI, Gemini's OpenAI-compatible route, LM Studio, and other compatible servers.

    Always ``max_tokens``. ``langchain-openai`` maps it to ``max_completion_tokens``
    for the models that require that spelling, so choosing by model name here
    would only duplicate (and eventually contradict) what the client already does.
    Compatible servers that do not know ``max_completion_tokens`` keep working.
    """

    supported = frozenset({"temperature", "top_p", "stop", "timeout_s"})
    names = {"max_output_tokens": "max_tokens", "timeout_s": "timeout"}


def adapter_for(provider: str = "", api_style: str = "", model: str = "") -> GenerationAdapter:
    name = (provider or "").strip().lower()
    if name == "ollama" or api_style == "ollama":
        return OllamaAdapter(model)
    if name == "anthropic" or api_style == "anthropic":
        return AnthropicAdapter(model)
    return OpenAICompatibleAdapter(model)
