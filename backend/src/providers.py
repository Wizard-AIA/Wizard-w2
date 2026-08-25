"""The table of every backend Wizard can address.

Provider handling used to be an ``if name == "ollama" ... elif`` chain repeated in
four places, plus a fifth copy of the local/cloud split in ``core/llm/resources``.
Everything backend-specific is now one row here; adding Groq or a self-hosted vLLM
is a row, not a code change.

Sits beside ``config`` rather than under ``core/llm/`` because ``Settings`` reads
this table at import time while ``core.llm.__init__`` imports ``settings`` back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ApiStyle = Literal["ollama", "openai", "anthropic"]
ProviderKind = Literal["local", "cloud"]


@dataclass(frozen=True)
class ProviderDescriptor:
    """One backend Wizard can address."""

    id: str
    label: str
    #: Where the model runs. The data-mode boundary is drawn on this, so it is
    #: declared per provider rather than inferred from a URL.
    kind: ProviderKind
    api_style: ApiStyle
    default_base_url: str = ""
    requires_key: bool = False
    #: Settings fields holding this provider's URL and key.
    url_field: str = ""
    key_field: str = ""
    #: Appended to the root for the OpenAI-compatible surface. Only LM Studio
    #: needs it: its root is bare because /api/v0 hangs off the same root.
    openai_suffix: str = ""
    hint: str = ""
    docs_url: str = ""


PROVIDER_TABLE: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        id="ollama",
        label="Ollama",
        kind="local",
        api_style="ollama",
        default_base_url="http://host.docker.internal:11434",
        url_field="OLLAMA_BASE_URL",
        hint="Models running on this machine through the Ollama daemon.",
        docs_url="https://ollama.com/download",
    ),
    ProviderDescriptor(
        id="lmstudio",
        label="LM Studio",
        kind="local",
        api_style="openai",
        default_base_url="http://host.docker.internal:1234",
        url_field="LMSTUDIO_BASE_URL",
        key_field="LMSTUDIO_API_KEY",
        openai_suffix="/v1",
        hint="Models running on this machine through LM Studio's local server.",
        docs_url="https://lmstudio.ai/",
    ),
    ProviderDescriptor(
        id="anthropic",
        label="Anthropic",
        kind="cloud",
        api_style="anthropic",
        default_base_url="https://api.anthropic.com/v1",
        requires_key=True,
        url_field="ANTHROPIC_BASE_URL",
        key_field="ANTHROPIC_API_KEY",
        hint="Claude models, called over the network. Your prompts leave this machine.",
        docs_url="https://console.anthropic.com/settings/keys",
    ),
    ProviderDescriptor(
        id="openai",
        label="OpenAI",
        kind="cloud",
        api_style="openai",
        default_base_url="https://api.openai.com/v1",
        requires_key=True,
        url_field="OPENAI_BASE_URL",
        key_field="OPENAI_API_KEY",
        hint="GPT models, called over the network. Your prompts leave this machine.",
        docs_url="https://platform.openai.com/api-keys",
    ),
    ProviderDescriptor(
        id="gemini",
        label="Gemini",
        kind="cloud",
        api_style="openai",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        requires_key=True,
        url_field="GEMINI_BASE_URL",
        key_field="GEMINI_API_KEY",
        hint="Gemini models, called over the network via Google's OpenAI-compatible endpoint. Your prompts leave this machine.",
        docs_url="https://aistudio.google.com/apikey",
    ),
    ProviderDescriptor(
        id="custom_gateway",
        label="Custom gateway",
        kind="cloud",
        api_style="openai",
        url_field="GATEWAY_API_URL",
        key_field="GATEWAY_API_KEY",
        hint="Any OpenAI-compatible endpoint — Groq, OpenRouter, Together, Gemini's compatibility route, or your own vLLM server.",
    ),
)

_BY_ID: dict[str, ProviderDescriptor] = {descriptor.id: descriptor for descriptor in PROVIDER_TABLE}

PROVIDERS: tuple[str, ...] = tuple(_BY_ID)
LOCAL_PROVIDERS: frozenset[str] = frozenset(d.id for d in PROVIDER_TABLE if d.kind == "local")
CLOUD_PROVIDERS: frozenset[str] = frozenset(d.id for d in PROVIDER_TABLE if d.kind == "cloud")


def describe(provider: str) -> ProviderDescriptor | None:
    """The descriptor for ``provider``, or ``None`` if there is no such backend."""
    return _BY_ID.get((provider or "").strip().lower())


def exists(provider: str) -> bool:
    return (provider or "").strip().lower() in _BY_ID


def is_cloud(provider: str) -> bool:
    """Whether a call to this provider leaves the machine.

    An unknown provider counts as cloud: this feeds the data-mode check, where
    treating something unrecognised as local would open the hole it exists to close.
    """
    descriptor = describe(provider)
    return descriptor is None or descriptor.kind == "cloud"


def is_local(provider: str) -> bool:
    return not is_cloud(provider)


def label_for(provider: str) -> str:
    descriptor = describe(provider)
    return descriptor.label if descriptor else provider


__all__ = [
    "CLOUD_PROVIDERS",
    "LOCAL_PROVIDERS",
    "PROVIDERS",
    "PROVIDER_TABLE",
    "ApiStyle",
    "ProviderDescriptor",
    "ProviderKind",
    "describe",
    "exists",
    "is_cloud",
    "is_local",
    "label_for",
]
