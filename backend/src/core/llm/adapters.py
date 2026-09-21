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
    supported = frozenset({"temperature", "top_p", "top_k", "stop", "timeout_s"})
    names = {
        "max_output_tokens": "max_tokens",
        "stop": "stop_sequences",
        "timeout_s": "timeout",
    }


class OpenAICompatibleAdapter(GenerationAdapter):
    supported = frozenset({"temperature", "top_p", "stop", "timeout_s"})
    names = {"timeout_s": "timeout", "stop": "stop"}

    def translate(self, config: GenerationConfig) -> AdapterPayload:
        result = super().translate(config)
        values = dict(result.values)
        # Reasoning-capable OpenAI models reject max_tokens in favor of the
        # current max_completion_tokens field. Standard models retain the
        # widely supported max_tokens spelling.
        output_name = (
            "max_completion_tokens"
            if any(marker in self.model.lower() for marker in ("o1", "o3", "o4", "gpt-5", "reasoning", "thinking"))
            else "max_tokens"
        )
        values.pop("max_output_tokens", None)
        values[output_name] = config.max_output_tokens
        return AdapterPayload(values, result.dropped)


class GeminiAdapter(OpenAICompatibleAdapter):
    """Google's native generation names, for callers using its native client."""

    supported = frozenset({"temperature", "top_p", "top_k", "stop", "timeout_s"})
    names = {"max_output_tokens": "max_output_tokens", "stop": "stop_sequences", "timeout_s": "timeout"}

    def translate(self, config: GenerationConfig) -> AdapterPayload:
        # The base implementation above is intentionally bypassed because the
        # Gemini native SDK names output and stop fields differently.
        values = {"max_output_tokens": config.max_output_tokens}
        dropped: dict[str, str] = {}
        for field, value in config.to_dict().items():
            if value is None or field == "max_output_tokens":
                continue
            if field in self.supported:
                values[self.names.get(field, field)] = value
            else:
                dropped[field] = "unsupported by provider"
        return AdapterPayload(values, dropped)


def adapter_for(provider: str = "", api_style: str = "", model: str = "") -> GenerationAdapter:
    name = (provider or "").strip().lower()
    if name == "ollama" or api_style == "ollama":
        return OllamaAdapter(model)
    if name == "anthropic" or api_style == "anthropic":
        return AnthropicAdapter(model)
    if name == "gemini":
        return GeminiAdapter(model)
    return OpenAICompatibleAdapter(model)
