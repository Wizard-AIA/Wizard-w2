"""Provider resolution, LM Studio discovery and per-role backend routing.

The invariant these protect: *which* backend a call goes to is a property of the
request, not of the process. Every helper here is exercised without touching a
network -- the HTTP layer is stubbed at ``_get_json``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.config import PROVIDERS, settings
from src.core.llm import LLMRole, llm_provider
from src.core.llm.provider import LLMProvider, LLMUnavailableError
from src.core.llm.registry import ModelRegistry, classify


# --------------------------------------------------------------------------- #
# Settings-level resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", PROVIDERS)
def test_every_declared_provider_resolves_to_itself(name: str) -> None:
    assert settings.resolve_provider(name) == name


@pytest.mark.parametrize("value", ["", None, "   ", "not-a-provider", "OLLAMA_TYPO"])
def test_unknown_provider_falls_back_to_the_configured_default(value: str | None) -> None:
    assert settings.resolve_provider(value) == settings.API_PROVIDER


def test_provider_lookup_is_case_insensitive() -> None:
    assert settings.resolve_provider("LMStudio") == "lmstudio"


@pytest.mark.parametrize(
    "configured",
    ["http://localhost:1234", "http://localhost:1234/", "http://localhost:1234/v1", "http://localhost:1234/v1/"],
)
def test_lmstudio_url_is_normalised_to_a_bare_root(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    """LM Studio's own UI shows the `/v1` form, so people paste that."""
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", settings.__class__._normalize_lmstudio_url(configured))
    assert settings.provider_root_url("lmstudio") == "http://localhost:1234"


def test_lmstudio_inference_url_carries_the_v1_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://localhost:1234")
    assert settings.provider_openai_base_url("lmstudio") == "http://localhost:1234/v1"


def test_gateway_url_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-configured gateway already includes its version segment."""
    monkeypatch.setattr(settings, "GATEWAY_API_URL", "https://gateway.example/v1")
    assert settings.provider_openai_base_url("custom_gateway") == "https://gateway.example/v1"


def test_api_keys_are_kept_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LMSTUDIO_API_KEY", "lms-key")
    monkeypatch.setattr(settings, "GATEWAY_API_KEY", "gw-key")
    assert settings.provider_api_key("lmstudio") == "lms-key"
    assert settings.provider_api_key("custom_gateway") == "gw-key"


def test_local_providers_are_configured_without_a_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GATEWAY_API_URL", "")
    assert settings.provider_is_configured("ollama") is True
    assert settings.provider_is_configured("lmstudio") is True
    assert settings.provider_is_configured("custom_gateway") is False


# --------------------------------------------------------------------------- #
# ModelSpec
# --------------------------------------------------------------------------- #
def test_resolve_targets_the_requested_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://localhost:1234")
    spec = llm_provider.resolve(LLMRole.WORKER, model="qwen-coder", provider="lmstudio")
    assert spec.provider == "lmstudio"
    assert spec.base_url == "http://localhost:1234/v1"


def test_resolve_without_a_provider_uses_the_default() -> None:
    spec = llm_provider.resolve(LLMRole.MANAGER)
    assert spec.provider == settings.API_PROVIDER


def test_ollama_spec_points_at_the_bare_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """ChatOllama builds its own paths; handing it a `/v1` base would break it."""
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")
    spec = llm_provider.resolve(LLMRole.MANAGER, provider="ollama")
    assert spec.base_url == "http://localhost:11434"


def test_same_model_name_on_two_providers_has_distinct_cache_keys() -> None:
    """Otherwise the first backend used would answer for the second one too."""
    first = llm_provider.resolve(LLMRole.WORKER, model="qwen2.5-coder", provider="ollama")
    second = llm_provider.resolve(LLMRole.WORKER, model="qwen2.5-coder", provider="lmstudio")
    assert first.cache_key() != second.cache_key()


def test_unavailable_message_names_the_endpoint() -> None:
    """'No client available' with no host is not actionable for a local daemon."""
    spec = llm_provider.resolve(LLMRole.WORKER, model="mystery", provider="lmstudio")
    message = llm_provider._unavailable_message(spec)
    assert "lmstudio" in message and spec.base_url in message and "mystery" in message


# --------------------------------------------------------------------------- #
# Cloud providers, resolved through the same path as the local ones
# --------------------------------------------------------------------------- #
def test_a_cloud_provider_resolves_like_any_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
    spec = llm_provider.resolve(LLMRole.MANAGER, model="claude-sonnet-4-5", provider="anthropic", data_mode="hybrid")
    assert spec.provider == "anthropic"
    assert spec.base_url == "https://api.anthropic.com/v1"
    assert spec.api_style == "anthropic"


def test_the_wire_dialect_comes_from_the_descriptor() -> None:
    """Nothing below `resolve` may branch on a provider name."""
    styles = {
        name: llm_provider.resolve(LLMRole.MANAGER, model="m", provider=name, data_mode="hybrid").api_style
        for name in PROVIDERS
    }
    assert styles == {
        "ollama": "ollama",
        "lmstudio": "openai",
        "anthropic": "anthropic",
        "openai": "openai",
        "gemini": "openai",
        "custom_gateway": "openai",
    }


def test_a_cloud_role_gets_no_keep_alive() -> None:
    """Keep-alive is an instruction to a local daemon. A hosted endpoint has no
    residency to manage, and sending one would be meaningless."""
    spec = llm_provider.resolve(LLMRole.MANAGER, model="claude-sonnet-4-5", provider="anthropic", data_mode="hybrid")
    assert spec.keep_alive == ""


def test_the_same_model_on_two_cloud_providers_has_distinct_cache_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GATEWAY_API_URL", "https://gateway.example/v1")
    first = llm_provider.resolve(LLMRole.MANAGER, model="gpt-4o", provider="openai", data_mode="hybrid")
    second = llm_provider.resolve(LLMRole.MANAGER, model="gpt-4o", provider="custom_gateway", data_mode="hybrid")
    assert first.cache_key() != second.cache_key()


def test_a_missing_key_is_reported_as_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not as an unreachable endpoint: the two have completely different fixes."""
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    spec = llm_provider.resolve(LLMRole.MANAGER, model="claude-sonnet-4-5", provider="anthropic", data_mode="hybrid")
    message = llm_provider._unavailable_message(spec)
    assert "API key" in message
    assert "ANTHROPIC_API_KEY" in message


def test_the_anthropic_client_is_actually_constructible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built for real, not mocked.

    Every other test in this file stubs the client out, so a wrong keyword here
    -- the output bound is `max_tokens_to_sample`, and `stop` must be passed --
    would only surface at runtime, on a machine with a real key, which is not
    this one. No network call is made: constructing the client does not talk to
    anything.
    """
    pytest.importorskip("langchain_anthropic", reason="optional extra; see requirements-optional.txt")

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    spec = llm_provider.resolve(
        LLMRole.MANAGER, model="claude-sonnet-4-5", provider="anthropic", max_tokens=1024, data_mode="hybrid"
    )
    client = llm_provider._build_client(spec)

    assert client is not None
    assert type(client).__name__ == "ChatAnthropic"
    assert getattr(client, "model", None) == "claude-sonnet-4-5"
    assert getattr(client, "max_tokens", None) == 1024


def test_a_missing_anthropic_package_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrade with an instruction, never with a bare "no client available"."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "langchain_anthropic":
            raise ImportError("No module named 'langchain_anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(builtins, "__import__", refuse)

    spec = llm_provider.resolve(LLMRole.MANAGER, model="claude-sonnet-4-5", provider="anthropic", data_mode="hybrid")
    with pytest.raises(LLMUnavailableError, match="langchain-anthropic"):
        llm_provider._build_client(spec)


def test_anthropic_discovery_sends_its_own_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic is not OpenAI-shaped: `x-api-key` and a required version header."""
    seen: dict[str, Any] = {}

    def fake_get(self, provider, url, headers=None, *, quiet=False):
        seen["url"] = url
        seen["headers"] = headers or {}
        return {"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]}

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(ModelRegistry, "_get_json", fake_get)

    models = ModelRegistry().list_models(provider="anthropic")
    assert [m.name for m in models] == ["claude-sonnet-4-5"]
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in seen["headers"]


def test_anthropic_without_a_key_says_so_instead_of_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unauthenticated request returns 401, which would be recorded as an
    unreachable host — pointing the user at the wrong problem."""
    called = False

    def fake_get(self, provider, url, headers=None, *, quiet=False):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(ModelRegistry, "_get_json", fake_get)

    registry = ModelRegistry()
    assert registry.list_models(provider="anthropic") == []
    assert called is False
    assert "key" in (registry.error_for("anthropic") or "")


def test_available_providers_describes_the_whole_table() -> None:
    entries = ModelRegistry().available_providers("hybrid")
    assert [entry["id"] for entry in entries] == list(PROVIDERS)
    assert all(entry["kind"] in ("local", "cloud") for entry in entries)
    assert all(entry["label"] for entry in entries)


def test_available_providers_marks_what_the_mode_forbids() -> None:
    entries = {entry["id"]: entry for entry in ModelRegistry().available_providers("local-only")}
    assert entries["ollama"]["allowed"] is True
    assert entries["anthropic"]["allowed"] is False


def test_available_providers_never_carries_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-secret-value")
    entries = ModelRegistry().available_providers("hybrid")
    assert "sk-ant-secret-value" not in str(entries)
    assert next(entry for entry in entries if entry["id"] == "anthropic")["has_key"] is True


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
LMSTUDIO_NATIVE_PAYLOAD: dict[str, Any] = {
    "data": [
        {
            "id": "qwen2.5-coder-7b-instruct",
            "type": "llm",
            "arch": "qwen2",
            "quantization": "Q4_K_M",
            "state": "loaded",
            "max_context_length": 32768,
        },
        {
            "id": "llava-v1.5-7b",
            "type": "vlm",
            "arch": "llama",
            "quantization": "Q4_0",
            "state": "not-loaded",
            "max_context_length": 4096,
        },
        {
            "id": "text-embedding-nomic-embed-text-v1.5",
            "type": "embeddings",
            "arch": "nomic-bert",
            "state": "not-loaded",
        },
    ]
}


def _registry_returning(payloads: dict[str, Any]) -> ModelRegistry:
    """A registry whose HTTP layer answers from a URL->payload map."""
    registry = ModelRegistry()
    calls: list[str] = []

    def fake_get(provider: str, url: str, headers: dict[str, str] | None = None, *, quiet: bool = False):
        calls.append(url)
        payload = payloads.get(url)
        if payload is None:
            registry._errors[provider] = f"Could not reach {provider} at {url}: refused"
        return payload

    registry._get_json = fake_get  # type: ignore[assignment]
    registry.calls = calls  # type: ignore[attr-defined]
    return registry


def test_lmstudio_native_metadata_beats_name_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    registry = _registry_returning({"http://lms:1234/api/v0/models": LMSTUDIO_NATIVE_PAYLOAD})

    models = {m.name: m for m in registry.list_models(provider="lmstudio")}

    coder = models["qwen2.5-coder-7b-instruct"]
    assert "code" in coder.capabilities  # name-derived hint merged in
    assert "chat" in coder.capabilities
    assert coder.quantization == "Q4_K_M"
    assert coder.context_length == 32768
    assert coder.loaded is True
    assert coder.provider == "lmstudio"

    # `type: vlm` is authoritative, not the substring match on the name.
    assert "vision" in models["llava-v1.5-7b"].capabilities
    assert models["llava-v1.5-7b"].loaded is False

    # An embedding model must never be offered as a chat model.
    assert models["text-embedding-nomic-embed-text-v1.5"].capabilities == ["embedding"]


def test_lmstudio_falls_back_to_the_openai_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything else serving that port speaks `/v1` but not `/api/v0`."""
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    registry = _registry_returning({"http://lms:1234/v1/models": {"data": [{"id": "some-model"}]}})

    models = registry.list_models(provider="lmstudio")

    assert [m.name for m in models] == ["some-model"]
    assert registry.calls == ["http://lms:1234/api/v0/models", "http://lms:1234/v1/models"]  # type: ignore[attr-defined]


def test_both_lmstudio_routes_failing_reports_the_native_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    registry = _registry_returning({})

    assert registry.list_models(provider="lmstudio") == []
    error = registry.error_for("lmstudio")
    assert error is not None and "api/v0/models" in error


def test_provider_lists_do_not_evict_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session may run its manager and worker on different backends."""
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    registry = _registry_returning(
        {
            "http://lms:1234/api/v0/models": LMSTUDIO_NATIVE_PAYLOAD,
            "http://ollama:11434/api/tags": {"models": [{"name": "deepseek-r1:1.5b", "size": 1_100_000_000}]},
        }
    )

    registry.list_models(provider="lmstudio")
    registry.list_models(provider="ollama")

    assert [m.name for m in registry.list_models(provider="ollama")] == ["deepseek-r1:1.5b"]
    assert len(registry.list_models(provider="lmstudio")) == 3


def test_errors_are_tracked_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    registry = _registry_returning(
        {"http://ollama:11434/api/tags": {"models": [{"name": "deepseek-r1:1.5b"}]}},
    )

    registry.list_models(provider="ollama")
    registry.list_models(provider="lmstudio")

    assert registry.error_for("ollama") is None
    assert registry.error_for("lmstudio") is not None


def test_available_providers_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """It renders on every page load; an offline host must not add a timeout."""
    registry = _registry_returning({})

    listed = registry.available_providers()

    assert registry.calls == []  # type: ignore[attr-defined]
    assert {entry["id"] for entry in listed} == set(PROVIDERS)
    assert sum(1 for entry in listed if entry["is_default"]) == 1


def test_suggestion_ignores_a_default_that_belongs_to_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`deepseek-r1:1.5b` is an Ollama tag; sending it to LM Studio is a 404."""
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    monkeypatch.setattr(settings, "MODEL_NAME", "deepseek-r1:1.5b")
    registry = _registry_returning({"http://lms:1234/api/v0/models": LMSTUDIO_NATIVE_PAYLOAD})

    suggested = registry.suggest(provider="lmstudio")

    assert suggested["manager"] != "deepseek-r1:1.5b"
    assert suggested["manager"] in {m.name for m in registry.list_models(provider="lmstudio")}
    assert suggested["worker"] == "qwen2.5-coder-7b-instruct"
    assert suggested["vision"] == "llava-v1.5-7b"


def test_ollama_discovery_still_reports_its_own_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    registry = _registry_returning(
        {
            "http://ollama:11434/api/tags": {
                "models": [
                    {
                        "name": "qwen2.5-coder:1.5b",
                        "size": 986_000_000,
                        "details": {"family": "qwen2", "parameter_size": "1.5B", "quantization_level": "Q4_K_M"},
                    }
                ]
            }
        }
    )

    (model,) = registry.list_models(provider="ollama")

    assert model.parameter_size == "1.5B"
    assert model.provider == "ollama"
    # Ollama does not report load state, so it must stay unknown rather than False.
    assert model.loaded is None


def test_classify_still_guesses_when_metadata_is_absent() -> None:
    assert "code" in classify("qwen2.5-coder-7b")
    assert classify("nomic-embed-text") == ["embedding"]


def test_a_failed_lookup_is_cached_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused TCP connect costs seconds on Windows, and one page load asks
    for both the list and a suggestion. Re-probing an offline provider each
    time made a switched-off backend visibly slow.
    """
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    registry = _registry_returning({})

    assert registry.list_models(provider="lmstudio") == []
    probes_after_first = len(registry.calls)  # type: ignore[attr-defined]
    assert registry.list_models(provider="lmstudio") == []

    assert len(registry.calls) == probes_after_first  # type: ignore[attr-defined]


def test_refresh_bypasses_the_failure_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "I just started LM Studio, refresh" has to work without waiting out a TTL."""
    monkeypatch.setattr(settings, "LMSTUDIO_BASE_URL", "http://lms:1234")
    payloads: dict[str, Any] = {}
    registry = _registry_returning(payloads)

    assert registry.list_models(provider="lmstudio") == []

    payloads["http://lms:1234/api/v0/models"] = LMSTUDIO_NATIVE_PAYLOAD
    assert registry.list_models(provider="lmstudio") == []  # still cached
    assert len(registry.list_models(force=True, provider="lmstudio")) == 3


# --------------------------------------------------------------------------- #
# Ollama client construction
# --------------------------------------------------------------------------- #
def test_the_ollama_client_keeps_models_resident_and_bounds_its_requests(monkeypatch) -> None:
    """Two properties the Ollama path lacked while the OpenAI path had both.

    `keep_alive`: the manager and worker alternate every iteration of the loop,
    so an eviction between them costs a full reload from disk each time. Ollama's
    own default is five minutes, which one slow turn can exceed while it is still
    running.

    A request timeout: `ChatOllama` has no `timeout` field, so `client_kwargs` is
    the only way to bound a call. Without it a wedged daemon hangs the turn
    forever -- which is the other half of "it also did not complete".
    """
    captured: dict = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", _FakeChatOllama)

    provider = LLMProvider()
    spec = provider.resolve(LLMRole.MANAGER, model="qwen2.5:3b", provider="ollama")
    assert provider._build_client(spec) is not None

    assert captured["keep_alive"] == settings.LLM_KEEP_ALIVE
    assert captured["client_kwargs"] == {"timeout": settings.LLM_REQUEST_TIMEOUT}
    assert captured["num_ctx"] == settings.LLM_NUM_CTX
    assert captured["num_thread"] == settings.LLM_NUM_THREAD


def test_a_per_call_output_budget_reaches_the_client(monkeypatch) -> None:
    """`num_predict` is the whole point of the per-purpose budgets."""
    captured: dict = {}

    class _FakeChatOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", _FakeChatOllama)

    provider = LLMProvider()
    spec = provider.resolve(LLMRole.MANAGER, model="qwen2.5:3b", provider="ollama", max_tokens=512)
    provider._build_client(spec)
    assert captured["num_predict"] == 512


def test_two_output_budgets_do_not_share_a_client() -> None:
    """The budget is part of the cache key, or the first one set would stick."""
    provider = LLMProvider()
    small = provider.resolve(LLMRole.MANAGER, model="m", provider="ollama", max_tokens=512)
    large = provider.resolve(LLMRole.MANAGER, model="m", provider="ollama", max_tokens=1536)
    assert small.cache_key() != large.cache_key()


# --------------------------------------------------------------------------- #
# What an unreachable provider tells the user
# --------------------------------------------------------------------------- #
def test_a_refused_connection_says_what_to_do_not_what_happened() -> None:
    """The OS message names the mechanism and nothing actionable.

    "[WinError 10061] No connection could be made because the target machine
    actively refused it" is true and useless. A refused connection to a local
    provider has exactly one meaning -- it is not running -- and the app knows
    which provider it asked.
    """
    import errno

    from src.core.llm.registry import unreachable_message

    refused = OSError()
    refused.errno = errno.ECONNREFUSED
    refused.winerror = 10061  # Windows does not raise ConnectionRefusedError here

    message = unreachable_message("lmstudio", "http://127.0.0.1:1234/api/v0/models", refused)

    assert "10061" not in message
    assert "Developer" in message, "it should say where the server switch actually is"
    assert "Serve on Local Network" in message, "the usual cause of an empty picker from Docker"


def test_a_refused_connection_is_recognised_on_posix_too() -> None:
    """Linux and macOS raise the errno subclass rather than a bare OSError."""
    from src.core.llm.registry import unreachable_message

    message = unreachable_message("ollama", "http://127.0.0.1:11434/api/tags", ConnectionRefusedError(61, "refused"))
    assert "ollama serve" in message


def test_a_timeout_is_not_reported_as_nothing_listening() -> None:
    """A slow provider and an absent one need different advice."""
    from src.core.llm.registry import unreachable_message

    message = unreachable_message("lmstudio", "http://host:1234/api", TimeoutError("timed out"))
    assert "Nothing is listening" not in message
    assert "starting up" in message


def test_an_unrecognised_failure_keeps_the_original_reason() -> None:
    """Guessing at a cause we have not identified would be worse than quoting it."""
    from src.core.llm.registry import unreachable_message

    message = unreachable_message("lmstudio", "http://host/api", "certificate verify failed")
    assert "certificate verify failed" in message
