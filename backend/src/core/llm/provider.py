"""Provider-agnostic LLM access with first-class token streaming.

Two things this replaces:

1. ``DataAnalysisAgent._get_llm`` / ``_get_worker_llm``, which cached exactly one
   manager and one worker client on the agent instance. That made per-request
   model selection impossible -- the first model chosen was the only model the
   process would ever use.
2. Blocking ``llm.invoke()`` calls. Every entry point here has a streaming twin
   so the UI can render tokens as they are produced instead of faking a reveal
   animation over an already-complete string.

Clients are cached by a (provider, endpoint, model, temperature, ...) key so
switching models is cheap and switching back reuses the warm client.

The provider is part of that key, and part of every call signature, because it
is a per-request choice rather than process-wide configuration: one analysis can
plan on an Ollama reasoning model and generate code on an LM Studio one.
``settings.API_PROVIDER`` is only the default used when a caller names none.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.config import settings
from src.core.data_mode import check_provider
from src.core.llm.resources import LOCAL_PROVIDERS, ResidentPlan, plan_for_models
from src.core.llm.usage import extract_usage, usage_ledger
from src.providers import describe
from src.utils.logging import logger


class LLMRole(StrEnum):
    """Which brain is being addressed. Determines the default model."""

    MANAGER = "manager"  # planning, critique, replanning
    WORKER = "worker"  # code generation
    VISION = "vision"  # plot description


@dataclass(frozen=True)
class ModelSpec:
    """A fully-resolved request for a specific model on a specific backend.

    The endpoint is captured here rather than read from ``settings`` inside
    ``_build_client``, because a single request can involve two providers -- a
    manager on Ollama and a worker on LM Studio, say -- and the cache key has to
    tell those apart.
    """

    provider: str
    model: str
    temperature: float
    max_tokens: int
    num_ctx: int
    base_url: str = ""
    api_key: str = ""
    #: Which wire dialect to speak. Carried here rather than re-derived in
    #: `_build_client` so nothing below branches on a provider name.
    api_style: str = "openai"
    #: How long the server should hold this model after the call. Derived from
    #: whether the manager and worker can share memory on this machine, so it is
    #: part of the cache key -- a client built when they fitted must not be
    #: reused once they do not.
    keep_alive: str = ""

    def cache_key(self) -> tuple:
        return (
            self.provider,
            self.base_url,
            self.model,
            self.temperature,
            self.max_tokens,
            self.num_ctx,
            self.keep_alive,
        )


class LLMUnavailableError(RuntimeError):
    """Raised when no client could be constructed for a request."""


class DataModeViolation(LLMUnavailableError):
    """Raised when the session's data mode forbids the provider a role resolved to.

    A subclass of the unavailable error so existing handlers already surface it,
    but distinguishable because this one is a policy decision rather than a fault.
    """


class LLMProvider:
    """Builds, caches and drives chat clients across Ollama and OpenAI-compatible gateways."""

    def __init__(self):
        self._clients: dict[tuple, Any] = {}
        self._plans: dict[tuple, ResidentPlan] = {}
        self._lock = threading.Lock()
        self._warming = False
        self._warmed = False  # becomes True after the attempt finishes, success or not

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def default_model_for(self, role: LLMRole, provider: str | None = None) -> str:
        """The model to use when the caller names none.

        A configured ``*_MODEL_NAME`` is an explicit pin and always wins. When it
        is empty -- the default -- the model is discovered from what the provider
        actually has installed, so the app runs against any model on any backend
        instead of failing on two hardcoded Ollama tags that do not exist
        anywhere else.
        """
        pinned = {
            LLMRole.WORKER: settings.WORKER_MODEL_NAME,
            LLMRole.VISION: settings.VISION_MODEL_NAME,
            LLMRole.MANAGER: settings.MODEL_NAME,
        }.get(role, settings.MODEL_NAME)
        if pinned.strip():
            return pinned.strip()

        # Imported lazily: `llm/__init__` loads this module before `registry`,
        # and discovery must not run as an import side effect.
        from src.core.llm.registry import model_registry

        try:
            return (model_registry.suggest(provider).get(role.value) or "").strip()
        except Exception as exc:  # pragma: no cover - discovery is best effort
            logger.warning("Model discovery failed while resolving a default", role=role.value, error=str(exc))
            return ""

    def resolve(
        self,
        role: LLMRole,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        provider: str | None = None,
        data_mode: str | None = None,
    ) -> ModelSpec:
        """Turns a role plus optional per-request overrides into a concrete spec.

        The data-mode boundary is enforced here because this is the one function
        every call already passes through, so a session that assigns its own
        provider per role cannot route around it.
        """
        resolved_provider = settings.resolve_provider(provider)

        refusal = check_provider(data_mode or "", resolved_provider, role.value)
        if refusal:
            raise DataModeViolation(refusal)

        descriptor = describe(resolved_provider)
        style = descriptor.api_style if descriptor else "openai"
        return ModelSpec(
            provider=resolved_provider,
            model=(model or self.default_model_for(role, resolved_provider)).strip(),
            temperature=settings.TEMPERATURE if temperature is None else float(temperature),
            max_tokens=max_tokens or settings.MAX_TOKENS,
            num_ctx=settings.LLM_NUM_CTX,
            keep_alive=self.keep_alive_for(resolved_provider),
            api_style=style,
            base_url=settings.provider_root_url(resolved_provider)
            if style == "ollama"
            else settings.provider_openai_base_url(resolved_provider),
            api_key=settings.provider_api_key(resolved_provider),
        )

    def resident_plan(self, provider: str | None = None) -> ResidentPlan:
        """Whether the models actually in play can be resident at once.

        Planned for the whole set that competes for this provider's memory --
        manager and worker always, vision when enabled, and the embedding
        model when it shares this provider -- not just the pair being
        resolved: the cost being avoided is one evicting another, which is a
        property of the whole set, not any one role.
        """
        resolved = settings.resolve_provider(provider)
        names = [
            self.default_model_for(LLMRole.MANAGER, resolved),
            self.default_model_for(LLMRole.WORKER, resolved),
        ]
        if settings.VISION_ENABLED:
            vision = self.default_model_for(LLMRole.VISION, resolved)
            if vision:
                names.append(vision)
        embedding_model = self._local_embedding_model(resolved)
        if embedding_model:
            names.append(embedding_model)

        key = (resolved, tuple(names), settings.LLM_NUM_CTX)
        cached = self._plans.get(key)
        if cached is None:
            cached = plan_for_models(names, resolved, settings.LLM_NUM_CTX)
            self._plans[key] = cached
            if not cached.co_resident:
                logger.info(
                    "Models will be swapped rather than co-resident",
                    reason=cached.reason,
                    keep_alive=cached.keep_alive,
                )
        return cached

    @staticmethod
    def _local_embedding_model(provider: str) -> str | None:
        """The embedding model's name, when resolved and sharing this provider.

        Lazily imported: `core.embeddings` reads this plan back for its own
        keep-alive (see `embeddings.py`), so this direction (llm -> embeddings)
        only happens inside the method body, never at module scope, to avoid
        inverting that dependency. Returns None until the embedding encoder
        has resolved at least once, or when it is on a different provider
        than the one being planned for.
        """
        try:
            from src.core.embeddings import embedding_service

            resolved = embedding_service.resolved_remote()
        except Exception:
            return None
        if resolved is None:
            return None
        embed_provider, embed_model = resolved
        return embed_model if embed_provider == provider else None

    def warm(self, *, block: bool = False, timeout: float | None = None) -> None:
        """Loads the manager and worker weights into memory ahead of the first question.

        Ollama and LM Studio do not load a model until something asks it a
        question, so on a fresh boot the first real turn pays for that load in
        the middle of answering. Warming pings each distinct model with a
        throwaway prompt instead, on a background thread, so the wait happens
        at boot.

        Reuses ``resident_plan`` for the model names and dedup rather than
        re-deriving them: it already resolves manager/worker per role and
        already collapses them to one entry when both roles use the same
        model, so a single-model setup -- the recommended one on a
        memory-constrained machine -- is warmed exactly once, not twice.

        Runs on a daemon thread so startup is never delayed. Never raises: a
        slow or absent daemon at boot degrades to the old lazy-load behaviour
        rather than failing startup.
        """
        if self._warmed or self._warming:
            return
        resolved = settings.resolve_provider(None)
        if resolved not in LOCAL_PROVIDERS:
            self._warmed = True
            return
        with self._lock:
            if self._warming or self._warmed:
                return
            self._warming = True

        def run() -> None:
            started = time.monotonic()
            warmed_names: list[str] = []
            try:
                warmed_names = asyncio.run(self._warm_models(resolved))
            except Exception as exc:  # noqa: BLE001 - warming must never take the app down
                logger.warning("LLM warm-up failed", provider=resolved, error=str(exc))
            finally:
                self._warming = False
                self._warmed = True
                logger.info(
                    "LLM warm-up finished",
                    provider=resolved,
                    models=warmed_names,
                    seconds=round(time.monotonic() - started, 1),
                )

        thread = threading.Thread(target=run, name="llm-warmup", daemon=True)
        thread.start()
        if block:
            thread.join(timeout)

    async def _warm_models(self, provider: str) -> list[str]:
        """Pings the manager and worker models, concurrently.

        Deliberately not derived from `resident_plan().footprints` -- that set
        can now also include the embedding model (see `resident_plan`), which
        must never be pinged with a chat completion. `embedding_service.warm()`
        already warms it correctly. Vision is not warmed here either, keeping
        `LLM_WARM_ON_STARTUP`'s scope exactly what it was before this method
        started sharing logic with the residency planner.
        """
        names = list(
            dict.fromkeys(
                name
                for name in (
                    self.default_model_for(LLMRole.MANAGER, provider),
                    self.default_model_for(LLMRole.WORKER, provider),
                )
                if name
            )
        )
        if not names:
            return []

        async def ping(name: str) -> None:
            try:
                await self.acomplete("Reply with OK.", model=name, provider=provider, max_tokens=4)
            except Exception as exc:  # noqa: BLE001 - one model's failure must not cancel the others
                logger.warning("Could not warm model", model=name, provider=provider, error=str(exc))

        await asyncio.gather(*(ping(name) for name in names))
        return names

    def release(
        self,
        role: LLMRole,
        model: str | None = None,
        provider: str | None = None,
        *,
        keep_if_shared_with: tuple[LLMRole, str | None] | None = None,
    ) -> None:
        """Best-effort, immediate unload of one role's model on Ollama.

        Fires `POST {root}/api/generate {"model": name, "keep_alive": 0}` with
        no prompt -- Ollama's documented immediate-unload technique -- on a
        daemon thread, and returns without waiting. Never raises, matching
        `warm()`. No-ops on every provider but Ollama: LM Studio manages its
        own residency and exposes no unload verb.

        `keep_if_shared_with` guards the one self-defeating case: when `role`
        and the named other role resolve to the same model -- a single model
        serving both roles -- releasing `role` would also evict the model the
        other role needs on its very next call.
        """
        if not settings.LLM_RELEASE_IDLE_MODELS:
            return
        resolved = settings.resolve_provider(provider)
        if resolved != "ollama":
            return
        name = (model or self.default_model_for(role, resolved)).strip()
        if not name:
            return
        if keep_if_shared_with is not None:
            other_role, other_model = keep_if_shared_with
            other_name = (other_model or self.default_model_for(other_role, resolved)).strip()
            if other_name and other_name == name:
                return

        def run() -> None:
            try:
                import httpx

                root = settings.provider_root_url(resolved).rstrip("/")
                httpx.post(f"{root}/api/generate", json={"model": name, "keep_alive": 0}, timeout=10.0)
                logger.debug("Released model", model=name, provider=resolved, role=role.value)
            except Exception as exc:  # noqa: BLE001 - release must never take the app down
                logger.debug("Could not release model", model=name, provider=resolved, error=str(exc))

        threading.Thread(target=run, name=f"llm-release-{role.value}", daemon=True).start()

    def keep_alive_for(self, provider: str) -> str:
        """How long this provider should hold a model after a call.

        Only Ollama takes a keep-alive. LM Studio manages residency itself and
        the gateways host the model elsewhere, so there is nothing to say.
        """
        if provider != "ollama":
            return ""
        try:
            return self.resident_plan(provider).keep_alive
        except Exception as exc:  # pragma: no cover - planning must never block a call
            logger.warning("Memory planning failed; using the default keep-alive", error=str(exc))
            return settings.LLM_KEEP_ALIVE

    # ------------------------------------------------------------------ #
    # Client construction
    # ------------------------------------------------------------------ #
    def get_client(self, spec: ModelSpec):
        key = spec.cache_key()
        client = self._clients.get(key)
        if client is not None:
            return client

        with self._lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            client = self._build_client(spec)
            if client is not None:
                self._clients[key] = client
            return client

    def _build_client(self, spec: ModelSpec):
        if not spec.model:
            # Reached when no model is pinned and discovery found nothing, i.e.
            # the daemon is down or has no models pulled. Constructing a client
            # for the empty string would fail later with a far worse message.
            logger.warning("No model resolved", provider=spec.provider, base_url=spec.base_url)
            return None
        try:
            if spec.api_style == "ollama":
                from langchain_ollama import ChatOllama

                logger.info("Initializing ChatOllama client", model=spec.model, temperature=spec.temperature)
                return ChatOllama(
                    model=spec.model,
                    base_url=spec.base_url or settings.OLLAMA_BASE_URL,
                    temperature=spec.temperature,
                    num_predict=spec.max_tokens,
                    num_ctx=spec.num_ctx,
                    num_thread=settings.LLM_NUM_THREAD,
                    repeat_penalty=1.1,
                    # The manager and worker alternate every iteration, so an
                    # eviction between them costs a full reload from disk each
                    # time. Ollama's own default is five minutes, which one slow
                    # turn can exceed while it is still running.
                    # Long when the manager and worker fit in memory together,
                    # short when they do not -- see `core/llm/resources.py`.
                    keep_alive=spec.keep_alive or settings.LLM_KEEP_ALIVE,
                    # ChatOllama has no `timeout` field, so this is the only way
                    # to bound a request. Without it a wedged daemon hangs the
                    # turn forever -- the OpenAI-compatible path has had a
                    # timeout all along and this one had none.
                    client_kwargs={"timeout": settings.LLM_REQUEST_TIMEOUT},
                )

            if spec.api_style == "anthropic":
                try:
                    from langchain_anthropic import ChatAnthropic
                except ImportError as exc:  # pragma: no cover - depends on optional extra
                    raise LLMUnavailableError(
                        "Anthropic support needs the `langchain-anthropic` package. "
                        "Install it with `uv pip install -r requirements-optional.txt`."
                    ) from exc

                logger.info("Initializing ChatAnthropic client", model=spec.model)
                # Assembled as a dict because the client's own annotations want a
                # SecretStr, which it coerces from a plain string at runtime.
                anthropic_kwargs: dict[str, Any] = {
                    "model_name": spec.model,
                    "base_url": spec.base_url or None,
                    "api_key": spec.api_key or None,
                    "temperature": spec.temperature,
                    # Anthropic requires an output bound; the per-purpose budget is it.
                    "max_tokens_to_sample": spec.max_tokens,
                    "timeout": settings.LLM_REQUEST_TIMEOUT,
                    "stop": None,
                }
                return ChatAnthropic(**anthropic_kwargs)

            try:
                from langchain_openai import ChatOpenAI
            except ImportError:  # pragma: no cover - depends on optional extra
                from langchain_community.chat_models import ChatOpenAI

            # LM Studio, vLLM, llama.cpp's server and hosted gateways all speak
            # this dialect. Note that context length is *not* sent: LM Studio
            # fixes it when the model is loaded, so LLM_NUM_CTX has no effect here.
            logger.info(
                "Initializing OpenAI-compatible client",
                provider=spec.provider,
                model=spec.model,
                base_url=spec.base_url or "<default>",
            )
            return ChatOpenAI(
                model=spec.model,
                base_url=spec.base_url or None,
                api_key=spec.api_key or "not-required",
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                timeout=settings.LLM_REQUEST_TIMEOUT,
            )
        except LLMUnavailableError:
            # A missing optional package names what to install; swallowing it here
            # would replace that with a generic "no client available".
            raise
        except Exception as exc:
            logger.error("Failed to construct LLM client", provider=spec.provider, model=spec.model, error=str(exc))
            return None

    def clear_cache(self):
        """Drops cached clients so new settings (base URL, temperature) take effect."""
        with self._lock:
            self._clients.clear()

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #
    def _record(self, spec: ModelSpec, role: LLMRole, session_id: str | None, response: Any, prompt: str, text: str):
        """Books one call against the session. Never raises — a meter must not fail a turn."""
        try:
            usage_ledger.record(
                session_id, spec.provider, spec.model, role.value, extract_usage(response, prompt, text)
            )
        except Exception as exc:  # pragma: no cover - accounting is best effort
            logger.warning("Could not record token usage", error=str(exc))

    def complete(
        self,
        prompt: str,
        role: LLMRole = LLMRole.MANAGER,
        model: str | None = None,
        temperature: float | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        data_mode: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Blocking completion. Returns "" when the provider is unreachable."""
        spec = self.resolve(
            role, model=model, temperature=temperature, provider=provider, max_tokens=max_tokens, data_mode=data_mode
        )
        client = self.get_client(spec)
        if client is None:
            raise LLMUnavailableError(self._unavailable_message(spec))
        try:
            response = client.invoke(prompt)
            text = self._extract_text(response)
            self._record(spec, role, session_id, response, prompt, text)
            return text
        except Exception as exc:
            logger.error("LLM completion failed", provider=spec.provider, model=spec.model, error=str(exc))
            raise LLMUnavailableError(str(exc)) from exc

    async def acomplete(
        self,
        prompt: str,
        role: LLMRole = LLMRole.MANAGER,
        model: str | None = None,
        temperature: float | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        data_mode: str | None = None,
        session_id: str | None = None,
    ) -> str:
        spec = self.resolve(
            role, model=model, temperature=temperature, provider=provider, max_tokens=max_tokens, data_mode=data_mode
        )
        client = self.get_client(spec)
        if client is None:
            raise LLMUnavailableError(self._unavailable_message(spec))
        try:
            response = await client.ainvoke(prompt)
            text = self._extract_text(response)
            self._record(spec, role, session_id, response, prompt, text)
            return text
        except Exception as exc:
            logger.error("LLM completion failed", provider=spec.provider, model=spec.model, error=str(exc))
            raise LLMUnavailableError(str(exc)) from exc

    async def astream(
        self,
        prompt: str,
        role: LLMRole = LLMRole.MANAGER,
        model: str | None = None,
        temperature: float | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        data_mode: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yields text deltas as the model produces them.

        Falls back to a single yield of the full response when the underlying
        client does not implement ``astream``.
        """
        spec = self.resolve(
            role, model=model, temperature=temperature, provider=provider, max_tokens=max_tokens, data_mode=data_mode
        )
        client = self.get_client(spec)
        if client is None:
            raise LLMUnavailableError(self._unavailable_message(spec))

        if not hasattr(client, "astream"):
            # `acomplete` books this call itself, so nothing is recorded here.
            yield await self.acomplete(
                prompt,
                role=role,
                model=model,
                temperature=temperature,
                provider=provider,
                max_tokens=max_tokens,
                data_mode=data_mode,
                session_id=session_id,
            )
            return

        # Usage arrives on one chunk, usually the last. Held so the whole stream
        # is booked exactly once, which is what `test_turn_cost` pins.
        produced: list[str] = []
        counted: Any = None
        try:
            async for chunk in client.astream(prompt):
                text = self._extract_text(chunk)
                if getattr(chunk, "usage_metadata", None) or getattr(chunk, "response_metadata", None):
                    counted = chunk
                if text:
                    produced.append(text)
                    yield text
        except Exception as exc:
            logger.error("LLM streaming failed", provider=spec.provider, model=spec.model, error=str(exc))
            raise LLMUnavailableError(str(exc)) from exc
        self._record(spec, role, session_id, counted, prompt, "".join(produced))

    async def stream_to(
        self,
        prompt: str,
        on_delta: Callable[[str], Any] | None = None,
        role: LLMRole = LLMRole.MANAGER,
        model: str | None = None,
        temperature: float | None = None,
        provider: str | None = None,
        max_tokens: int | None = None,
        data_mode: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Streams a completion, invoking ``on_delta`` per chunk, and returns the full text.

        ``on_delta`` may be sync or async; both are supported so callers can push
        straight into a WebSocket without wrapping. Usage is booked by ``astream``,
        so this must not book it again.
        """
        buffer: list[str] = []
        async for delta in self.astream(
            prompt,
            role=role,
            model=model,
            temperature=temperature,
            provider=provider,
            max_tokens=max_tokens,
            data_mode=data_mode,
            session_id=session_id,
        ):
            buffer.append(delta)
            if on_delta is not None:
                result = on_delta(delta)
                if asyncio.iscoroutine(result):
                    _ = await result
        return "".join(buffer)

    async def describe_image(
        self,
        base64_png: str,
        model: str | None = None,
        provider: str | None = None,
        data_mode: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Multimodal description of a rendered chart."""
        spec = self.resolve(LLMRole.VISION, model=model, temperature=0.2, provider=provider, data_mode=data_mode)
        client = self.get_client(spec)
        if client is None:
            raise LLMUnavailableError(self._unavailable_message(spec))

        from langchain_core.messages import HumanMessage

        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        "Describe this data visualization in 2-3 sentences. "
                        "Explain the visible trend, the axes, and any key insight."
                    ),
                },
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_png}"}},
            ]
        )
        response = await client.ainvoke([message])
        text = self._extract_text(response).strip()
        self._record(spec, LLMRole.VISION, session_id, response, base64_png, text)
        return text

    # ------------------------------------------------------------------ #
    @staticmethod
    def _unavailable_message(spec: ModelSpec) -> str:
        """Names the endpoint, since 'no client available' alone is undebuggable.

        The usual cause is a local daemon that is not running, and the user can
        only check that if they are told which host was tried.
        """
        where = spec.base_url or "the configured endpoint"
        descriptor = describe(spec.provider)
        if descriptor is not None and descriptor.requires_key and not spec.api_key:
            return (
                f"{descriptor.label} needs an API key and none is set. "
                f"Add one on the Models page, or set {descriptor.key_field} in backend/.env."
            )
        if not spec.model:
            return (
                f"No model is available on {spec.provider} at {where}. "
                "Nothing is pinned in the configuration and discovery found none installed."
            )
        return f"No LLM client available for '{spec.model}' on {spec.provider} at {where}"

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Normalises the several shapes LangChain returns into plain text."""
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        content = getattr(response, "content", None)
        if content is None:
            return str(response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            return "".join(parts)
        return str(content)


llm_provider = LLMProvider()
