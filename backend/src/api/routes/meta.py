"""Health, capability discovery and model selection."""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import JSONResponse

from src.api.deps import get_credential_store, get_session, require_api_key
from src.api.schemas import (
    DataModeRequest,
    DataModeResponse,
    DatasetPolicyRequest,
    HealthDetailResponse,
    HealthResponse,
    ModelDownloadRequest,
    ModelDownloadsResponse,
    ModelDownloadState,
    ModelInfoResponse,
    ModelListResponse,
    ModelSelection,
    PermissionCategoryResponse,
    PermissionsRequest,
    PermissionsResponse,
    ProviderCredentialRequest,
    ProviderDownloadCapability,
    ProviderInfo,
    ProvidersResponse,
    ServerConfig,
    SessionResponse,
    UpdateConfigPayload,
    UsageResponse,
)
from src.config import settings
from src.core.credentials import CredentialStore
from src.core.data_mode import allowed_providers, check_provider, describe_mode, disabled_tools
from src.core.database import db_mgr
from src.core.embeddings import embedding_service
from src.core.execution import isolation_for
from src.core.infra.cache import get_cache
from src.core.infra.queue import get_queue
from src.core.ingest.documents import supported_document_extensions
from src.core.ingest.loader import DatasetLoader
from src.core.llm import llm_provider, model_registry, usage_ledger
from src.core.llm.downloader import ProviderNotDownloadable, model_downloader
from src.core.llm.reasoning import looks_like_reasoning_model
from src.core.permissions import CATEGORIES, describe_profile, normalize as normalize_profile
from src.core.security.sandbox import capability as sandbox_capability
from src.core.session import Session
from src.core.tools import runtime as runtime_backend
from src.providers import exists as provider_exists
from src.utils.hostinfo import host_info
from src.utils.logging import logger


router = APIRouter(tags=["meta"])

API_VERSION = "4.0.0"


@router.get("/metrics")
async def metrics_endpoint():
    from starlette.responses import PlainTextResponse

    from src.core.infra.metrics import metrics

    return PlainTextResponse(content=metrics.generate_prometheus_text(), media_type="text/plain; version=0.0.4")


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready", response_model=HealthDetailResponse)
async def health_ready() -> JSONResponse:
    backend = runtime_backend.active_backend()
    sandbox_available = backend == "docker"
    checks: dict[str, str] = {}
    is_ready = True

    try:
        with db_mgr._read() as conn:
            conn.execute("SELECT 1")
        checks["sqlite"] = "ok"
    except Exception as e:
        checks["sqlite"] = str(e)
        is_ready = False

    if settings.REDIS_URL:
        try:
            cache = get_cache()
            if hasattr(cache, "_client"):
                if cache._client.ping():
                    checks["redis"] = "ok"
                else:
                    checks["redis"] = "ping failed"
                    is_ready = False
            else:
                checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = str(e)
            is_ready = False

    if sandbox_available:
        try:
            import httpx

            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")) as client:
                resp = await client.get("http://localhost/_ping", timeout=2.0)
                if resp.status_code == 200:
                    checks["docker"] = "ok"
                else:
                    checks["docker"] = f"status {resp.status_code}"
                    is_ready = False
        except Exception as e:
            checks["docker"] = str(e)
            is_ready = False

    status_code = 200 if is_ready else 503
    status = "ok" if is_ready else "degraded"

    return JSONResponse(
        status_code=status_code,
        content=HealthDetailResponse(
            status=status,
            version=API_VERSION,
            app_version="1.0.6",
            sandbox_available=sandbox_available,
            execution_backend=backend,
            model_provider=settings.API_PROVIDER,
            checks=checks,
        ).model_dump(),
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> JSONResponse:
    return await health_ready()


def performance_notes() -> list[str]:
    """Configuration that will make this install slow, named in plain language.

    The backend knows why a turn is expensive and the user does not, and the
    answer to "why did that take twenty minutes" should be one screen rather than
    a support conversation or a read through `config.py`.

    Each note is checked by its *symptom*, not by whether the setting was pinned:
    the host-sizing validator assigns to these fields, so `model_fields_set` no
    longer distinguishes a user's choice from a derived one by the time anybody
    can ask. Comparing against the measured machine works either way.
    """
    host = host_info()
    notes: list[str] = []

    if looks_like_reasoning_model(settings.MODEL_NAME):
        notes.append(
            f"MODEL_NAME ({settings.MODEL_NAME}) looks like a reasoning model. It is called three to five "
            "times per question, and it thinks at length before each answer. Its thinking is stripped and "
            "never reaches the answer, but you still wait for it — a plain instruct model is much faster here."
        )

    if settings.LLM_NUM_THREAD > host.cores:
        notes.append(
            f"LLM_NUM_THREAD is {settings.LLM_NUM_THREAD} on {host.cores} physical cores. Local inference is "
            "memory-bandwidth bound, so extra threads add contention rather than throughput. Remove it from "
            "backend/.env to have it measured."
        )

    if host.profile == "laptop" and settings.LLM_NUM_CTX > 8192:
        notes.append(
            f"LLM_NUM_CTX is {settings.LLM_NUM_CTX:,} on a laptop-class machine. This reserves KV cache for "
            "every resident model, and the manager and worker alternate every step — so one gets evicted and "
            "reloaded from disk each time. Prompts here stay under 8k. Remove it from backend/.env."
        )

    plan = _resident_plan()
    if plan is not None and not plan.co_resident:
        notes.append(
            f"The manager and worker need about {plan.required_gb:.1f} GB together, more than the "
            f"{plan.budget_gb:.1f} GB budgeted from this machine's memory. Each model is now released after "
            "it runs rather than competing for RAM, which costs one reload per step but avoids swapping. "
            "A smaller model for one of the two roles, or the same model for both, removes the reload."
        )
    if plan is not None and not plan.fits:
        notes.append(
            f"The largest configured model needs more memory on its own ({plan.required_gb:.1f} GB) than this "
            f"machine can give it ({plan.budget_gb:.1f} GB). Expect the operating system to page it to disk, "
            "which is far slower than a smaller model would be. Choose a smaller model or a heavier quantization."
        )

    if settings.resolve_provider(None) == "ollama":
        slots_note = _recommended_ollama_slots_note(plan)
        if slots_note:
            notes.append(slots_note)

    return notes


def _resident_plan():
    """The memory plan, or ``None`` when it cannot be worked out.

    Never raises: this feeds a diagnostics panel, and a panel that fails is
    worse than a panel with one fewer line on it.
    """
    try:
        return llm_provider.resident_plan()
    except Exception as exc:  # pragma: no cover - diagnostics are best effort
        logger.warning("Could not build the memory plan", error=str(exc))
        return None


def _memory_plan_dict() -> dict | None:
    plan = _resident_plan()
    return None if plan is None else plan.to_dict()


def _recommended_ollama_slots_note(plan) -> str | None:
    """Names the OLLAMA_MAX_LOADED_MODELS this install could use. Informational only.

    There is no API to read Ollama's actual configured ceiling off an
    already-running external daemon -- it is a server-process env var, not a
    client-reachable setting. This process's own environment is read as a
    best-effort hint and explicitly caveated: Ollama is very often a
    different process, container or machine, whose environment this backend
    cannot see at all.
    """
    if plan is None or len(plan.footprints) < 2:
        return None
    needed = len(plan.footprints)
    roles = ", ".join(fp.name for fp in plan.footprints)
    configured = os.environ.get("OLLAMA_MAX_LOADED_MODELS", "").strip()
    if configured.isdigit() and int(configured) < needed:
        return (
            f"This install can have {needed} distinct local models in play at once ({roles}), but "
            f"OLLAMA_MAX_LOADED_MODELS is {configured} in this process's own environment — which may not "
            f"be the Ollama server's, if it runs elsewhere. Models will be swapped between that many slots; "
            f"consider raising it to at least {needed} on the machine actually running Ollama."
        )
    if not configured:
        return (
            f"This install can have {needed} distinct local models in play at once ({roles}). If the Ollama "
            f"server's own OLLAMA_MAX_LOADED_MODELS is lower than that, expect swapping between them even "
            f"when this app releases models proactively; setting OLLAMA_MAX_LOADED_MODELS={needed} on the "
            f"machine running Ollama removes that."
        )
    return None


@router.get("/api/config", response_model=ServerConfig)
async def server_config() -> ServerConfig:
    """Everything the client needs to render the right controls."""
    host = host_info()
    backend = runtime_backend.active_backend()
    return ServerConfig(
        app_name=settings.APP_NAME,
        version=API_VERSION,
        app_version="1.0.6",
        plot_format=settings.PLOT_FORMAT,
        sandbox_available=backend == "docker",
        sandbox_enabled=settings.SANDBOX_ENABLED,
        execution_backend=cast(Any, backend),
        execution_backend_setting=settings.EXECUTION_BACKEND,
        execution_isolation=isolation_for(backend),
        host_sandbox=settings.HOST_SANDBOX,
        # What this machine *can* enforce. Network-free and cheap; proving it
        # was enforced is `GET /api/sandbox/selftest`, which spawns a probe.
        sandbox_capability=sandbox_capability.detect().to_dict(),
        sandbox_tier=settings.SANDBOX_TIER,
        system_profile=settings.system_profile,
        host_cores=host.cores,
        host_ram_gb=None if host.ram_gb is None else round(host.ram_gb, 1),
        sandbox_mem_limit=settings.SANDBOX_MEM_LIMIT,
        max_sessions=settings.SESSION_MAX_ACTIVE,
        model_provider=settings.API_PROVIDER,
        supported_formats=DatasetLoader.supported_extensions(),
        max_upload_mb=settings.MAX_UPLOAD_BYTES // (1024 * 1024),
        queue_backend=get_queue().backend_name,
        cache_backend=get_cache().name,
        embeddings_semantic=embedding_service.is_semantic,
        embeddings_backend=embedding_service.backend,
        rag_enabled=settings.RAG_ENABLED,
        council_enabled=settings.COUNCIL_ENABLED,
        requires_api_key=bool(settings.API_KEY),
        agent_tier=settings.AGENT_TIER,
        agent_max_iterations=settings.AGENT_MAX_ITERATIONS,
        agent_require_approval=settings.AGENT_REQUIRE_APPROVAL,
        agent_permission_profile=settings.AGENT_PERMISSION_PROFILE,
        agent_consent_timeout=settings.AGENT_CONSENT_TIMEOUT,
        agent_verify=settings.AGENT_VERIFY,
        agent_grounding_check=settings.AGENT_GROUNDING_CHECK,
        context_docs_enabled=settings.CONTEXT_DOCS_ENABLED,
        supported_document_formats=supported_document_extensions(),
        agent_turn_timeout=settings.AGENT_TURN_TIMEOUT,
        llm_num_thread=settings.LLM_NUM_THREAD,
        llm_num_ctx=settings.LLM_NUM_CTX,
        llm_keep_alive=settings.LLM_KEEP_ALIVE,
        memory_plan=_memory_plan_dict(),
        performance_notes=performance_notes(),
        data_mode=settings.data_mode,
        data_schema_only=settings.DATA_SCHEMA_ONLY,
        temperature=settings.TEMPERATURE,
        max_tokens=settings.MAX_TOKENS,
        subagent_enabled=settings.SUBAGENT_ENABLED,
        subagent_max_iterations=settings.SUBAGENT_MAX_ITERATIONS,
        agent_emit_script=settings.AGENT_EMIT_SCRIPT,
        ollama_base_url=settings.OLLAMA_BASE_URL,
        lmstudio_base_url=settings.LMSTUDIO_BASE_URL,
        openai_base_url=settings.OPENAI_BASE_URL,
        anthropic_base_url=settings.ANTHROPIC_BASE_URL,
        gemini_base_url=settings.GEMINI_BASE_URL,
        gateway_api_url=settings.GATEWAY_API_URL,
        host_sandbox_network=settings.HOST_SANDBOX_NETWORK,
        sandbox_exec_timeout=settings.SANDBOX_EXEC_TIMEOUT,
        vision_enabled=settings.VISION_ENABLED,
        skills_enabled=settings.SKILLS_ENABLED,
        api_provider=settings.API_PROVIDER,
        embedding_provider=settings.EMBEDDING_PROVIDER,
        embedding_model=settings.EMBEDDING_REMOTE_MODEL,
        embeddings_remote_enabled=settings.EMBEDDINGS_REMOTE_ENABLED,
    )


def _persist_env_file(updates: dict[str, str]) -> None:
    """Safely updates or appends key-value pairs in the .env file."""
    from pathlib import Path

    env_paths = [
        settings.BASE_DIR / "backend" / ".env",
        settings.BASE_DIR / ".env",
        Path.cwd() / "backend" / ".env",
        Path.cwd() / ".env",
    ]
    target_path = next((p for p in env_paths if p.is_file()), env_paths[0])
    target_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if target_path.exists():
        try:
            lines = target_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []

    remaining_updates = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in remaining_updates:
                val = remaining_updates.pop(k)
                new_lines.append(f"{k}={val}")
                continue
        new_lines.append(line)

    for k, val in remaining_updates.items():
        new_lines.append(f"{k}={val}")

    try:
        target_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write .env update to disk", error=str(exc), path=str(target_path))


@router.patch("/api/config", response_model=ServerConfig, dependencies=[Depends(require_api_key)])
@router.post("/api/config", response_model=ServerConfig, dependencies=[Depends(require_api_key)])
async def update_server_config(
    payload: UpdateConfigPayload,
    credentials: CredentialStore = Depends(get_credential_store),
) -> ServerConfig:
    """Mutates runtime configuration and persists updates to .env and credentials."""
    env_updates: dict[str, str] = {}

    if payload.execution_backend is not None:
        settings.EXECUTION_BACKEND = payload.execution_backend
        os.environ["EXECUTION_BACKEND"] = payload.execution_backend
        env_updates["EXECUTION_BACKEND"] = payload.execution_backend

    if payload.host_sandbox is not None:
        settings.HOST_SANDBOX = payload.host_sandbox
        os.environ["HOST_SANDBOX"] = payload.host_sandbox
        env_updates["HOST_SANDBOX"] = payload.host_sandbox

    if payload.host_sandbox_network is not None:
        settings.HOST_SANDBOX_NETWORK = payload.host_sandbox_network
        os.environ["HOST_SANDBOX_NETWORK"] = payload.host_sandbox_network
        env_updates["HOST_SANDBOX_NETWORK"] = payload.host_sandbox_network

    if payload.sandbox_tier is not None:
        settings.SANDBOX_TIER = payload.sandbox_tier
        os.environ["SANDBOX_TIER"] = payload.sandbox_tier
        env_updates["SANDBOX_TIER"] = payload.sandbox_tier

    if payload.sandbox_mem_limit is not None:
        settings.SANDBOX_MEM_LIMIT = payload.sandbox_mem_limit
        os.environ["SANDBOX_MEM_LIMIT"] = payload.sandbox_mem_limit
        env_updates["SANDBOX_MEM_LIMIT"] = payload.sandbox_mem_limit

    if payload.max_upload_mb is not None:
        settings.MAX_UPLOAD_BYTES = payload.max_upload_mb * 1024 * 1024
        os.environ["MAX_UPLOAD_BYTES"] = str(settings.MAX_UPLOAD_BYTES)
        env_updates["MAX_UPLOAD_BYTES"] = str(settings.MAX_UPLOAD_BYTES)

    if payload.plot_format is not None:
        settings.PLOT_FORMAT = payload.plot_format
        os.environ["PLOT_FORMAT"] = payload.plot_format
        env_updates["PLOT_FORMAT"] = payload.plot_format

    if payload.agent_tier is not None:
        settings.AGENT_TIER = payload.agent_tier
        os.environ["AGENT_TIER"] = payload.agent_tier
        env_updates["AGENT_TIER"] = payload.agent_tier

    if payload.agent_max_iterations is not None:
        settings.AGENT_MAX_ITERATIONS = payload.agent_max_iterations
        os.environ["AGENT_MAX_ITERATIONS"] = str(payload.agent_max_iterations)
        env_updates["AGENT_MAX_ITERATIONS"] = str(payload.agent_max_iterations)

    if payload.agent_turn_timeout is not None:
        settings.AGENT_TURN_TIMEOUT = payload.agent_turn_timeout
        os.environ["AGENT_TURN_TIMEOUT"] = str(payload.agent_turn_timeout)
        env_updates["AGENT_TURN_TIMEOUT"] = str(payload.agent_turn_timeout)

    if payload.agent_require_approval is not None:
        settings.AGENT_REQUIRE_APPROVAL = payload.agent_require_approval
        os.environ["AGENT_REQUIRE_APPROVAL"] = str(payload.agent_require_approval).lower()
        env_updates["AGENT_REQUIRE_APPROVAL"] = str(payload.agent_require_approval).lower()

    if payload.agent_verify is not None:
        settings.AGENT_VERIFY = payload.agent_verify
        os.environ["AGENT_VERIFY"] = str(payload.agent_verify).lower()
        env_updates["AGENT_VERIFY"] = str(payload.agent_verify).lower()

    if payload.agent_grounding_check is not None:
        settings.AGENT_GROUNDING_CHECK = payload.agent_grounding_check
        os.environ["AGENT_GROUNDING_CHECK"] = str(payload.agent_grounding_check).lower()
        env_updates["AGENT_GROUNDING_CHECK"] = str(payload.agent_grounding_check).lower()

    if payload.agent_emit_script is not None:
        settings.AGENT_EMIT_SCRIPT = payload.agent_emit_script
        os.environ["AGENT_EMIT_SCRIPT"] = str(payload.agent_emit_script).lower()
        env_updates["AGENT_EMIT_SCRIPT"] = str(payload.agent_emit_script).lower()

    if payload.subagent_enabled is not None:
        settings.SUBAGENT_ENABLED = payload.subagent_enabled
        os.environ["SUBAGENT_ENABLED"] = str(payload.subagent_enabled).lower()
        env_updates["SUBAGENT_ENABLED"] = str(payload.subagent_enabled).lower()

    if payload.subagent_max_iterations is not None:
        settings.SUBAGENT_MAX_ITERATIONS = payload.subagent_max_iterations
        os.environ["SUBAGENT_MAX_ITERATIONS"] = str(payload.subagent_max_iterations)
        env_updates["SUBAGENT_MAX_ITERATIONS"] = str(payload.subagent_max_iterations)

    if payload.temperature is not None:
        settings.TEMPERATURE = payload.temperature
        os.environ["TEMPERATURE"] = str(payload.temperature)
        env_updates["TEMPERATURE"] = str(payload.temperature)

    if payload.max_tokens is not None:
        settings.MAX_TOKENS = payload.max_tokens
        os.environ["MAX_TOKENS"] = str(payload.max_tokens)
        env_updates["MAX_TOKENS"] = str(payload.max_tokens)

    if payload.llm_num_thread is not None:
        settings.LLM_NUM_THREAD = payload.llm_num_thread
        os.environ["LLM_NUM_THREAD"] = str(payload.llm_num_thread)
        env_updates["LLM_NUM_THREAD"] = str(payload.llm_num_thread)

    if payload.llm_num_ctx is not None:
        settings.LLM_NUM_CTX = payload.llm_num_ctx
        os.environ["LLM_NUM_CTX"] = str(payload.llm_num_ctx)
        env_updates["LLM_NUM_CTX"] = str(payload.llm_num_ctx)

    if payload.llm_keep_alive is not None:
        settings.LLM_KEEP_ALIVE = payload.llm_keep_alive
        os.environ["LLM_KEEP_ALIVE"] = payload.llm_keep_alive
        env_updates["LLM_KEEP_ALIVE"] = payload.llm_keep_alive

    if payload.rag_enabled is not None:
        settings.RAG_ENABLED = payload.rag_enabled
        os.environ["RAG_ENABLED"] = str(payload.rag_enabled).lower()
        env_updates["RAG_ENABLED"] = str(payload.rag_enabled).lower()

    if payload.ollama_base_url is not None:
        settings.OLLAMA_BASE_URL = payload.ollama_base_url
        os.environ["OLLAMA_BASE_URL"] = payload.ollama_base_url
        env_updates["OLLAMA_BASE_URL"] = payload.ollama_base_url

    if payload.lmstudio_base_url is not None:
        settings.LMSTUDIO_BASE_URL = payload.lmstudio_base_url
        os.environ["LMSTUDIO_BASE_URL"] = payload.lmstudio_base_url
        env_updates["LMSTUDIO_BASE_URL"] = payload.lmstudio_base_url

    if payload.openai_base_url is not None:
        settings.OPENAI_BASE_URL = payload.openai_base_url
        os.environ["OPENAI_BASE_URL"] = payload.openai_base_url
        env_updates["OPENAI_BASE_URL"] = payload.openai_base_url

    if payload.openai_api_key is not None:
        settings.OPENAI_API_KEY = payload.openai_api_key
        os.environ["OPENAI_API_KEY"] = payload.openai_api_key
        credentials.set("openai", payload.openai_api_key)
        env_updates["OPENAI_API_KEY"] = payload.openai_api_key

    if payload.anthropic_base_url is not None:
        settings.ANTHROPIC_BASE_URL = payload.anthropic_base_url
        os.environ["ANTHROPIC_BASE_URL"] = payload.anthropic_base_url
        env_updates["ANTHROPIC_BASE_URL"] = payload.anthropic_base_url

    if payload.anthropic_api_key is not None:
        settings.ANTHROPIC_API_KEY = payload.anthropic_api_key
        os.environ["ANTHROPIC_API_KEY"] = payload.anthropic_api_key
        credentials.set("anthropic", payload.anthropic_api_key)
        env_updates["ANTHROPIC_API_KEY"] = payload.anthropic_api_key

    if payload.api_provider is not None:
        settings.API_PROVIDER = payload.api_provider
        os.environ["API_PROVIDER"] = payload.api_provider
        env_updates["API_PROVIDER"] = payload.api_provider

    if payload.data_mode is not None:
        settings.DATA_MODE = payload.data_mode
        os.environ["DATA_MODE"] = payload.data_mode
        env_updates["DATA_MODE"] = payload.data_mode

    if payload.data_schema_only is not None:
        settings.DATA_SCHEMA_ONLY = payload.data_schema_only
        os.environ["DATA_SCHEMA_ONLY"] = str(payload.data_schema_only).lower()
        env_updates["DATA_SCHEMA_ONLY"] = str(payload.data_schema_only).lower()

    if payload.sandbox_exec_timeout is not None:
        settings.SANDBOX_EXEC_TIMEOUT = payload.sandbox_exec_timeout
        os.environ["SANDBOX_EXEC_TIMEOUT"] = str(payload.sandbox_exec_timeout)
        env_updates["SANDBOX_EXEC_TIMEOUT"] = str(payload.sandbox_exec_timeout)

    if payload.council_enabled is not None:
        settings.COUNCIL_ENABLED = payload.council_enabled
        os.environ["COUNCIL_ENABLED"] = str(payload.council_enabled).lower()
        env_updates["COUNCIL_ENABLED"] = str(payload.council_enabled).lower()

    if payload.vision_enabled is not None:
        settings.VISION_ENABLED = payload.vision_enabled
        os.environ["VISION_ENABLED"] = str(payload.vision_enabled).lower()
        env_updates["VISION_ENABLED"] = str(payload.vision_enabled).lower()

    if payload.context_docs_enabled is not None:
        settings.CONTEXT_DOCS_ENABLED = payload.context_docs_enabled
        os.environ["CONTEXT_DOCS_ENABLED"] = str(payload.context_docs_enabled).lower()
        env_updates["CONTEXT_DOCS_ENABLED"] = str(payload.context_docs_enabled).lower()

    if payload.skills_enabled is not None:
        settings.SKILLS_ENABLED = payload.skills_enabled
        os.environ["SKILLS_ENABLED"] = str(payload.skills_enabled).lower()
        env_updates["SKILLS_ENABLED"] = str(payload.skills_enabled).lower()

    if payload.gemini_base_url is not None:
        settings.GEMINI_BASE_URL = payload.gemini_base_url
        os.environ["GEMINI_BASE_URL"] = payload.gemini_base_url
        env_updates["GEMINI_BASE_URL"] = payload.gemini_base_url

    if payload.gemini_api_key is not None:
        settings.GEMINI_API_KEY = payload.gemini_api_key
        os.environ["GEMINI_API_KEY"] = payload.gemini_api_key
        credentials.set("gemini", payload.gemini_api_key)
        env_updates["GEMINI_API_KEY"] = payload.gemini_api_key

    if payload.gateway_api_url is not None:
        settings.GATEWAY_API_URL = payload.gateway_api_url
        os.environ["GATEWAY_API_URL"] = payload.gateway_api_url
        env_updates["GATEWAY_API_URL"] = payload.gateway_api_url

    if payload.gateway_api_key is not None:
        settings.GATEWAY_API_KEY = payload.gateway_api_key
        os.environ["GATEWAY_API_KEY"] = payload.gateway_api_key
        credentials.set("custom_gateway", payload.gateway_api_key)
        env_updates["GATEWAY_API_KEY"] = payload.gateway_api_key

    if payload.embedding_provider is not None:
        settings.EMBEDDING_PROVIDER = payload.embedding_provider
        os.environ["EMBEDDING_PROVIDER"] = payload.embedding_provider
        env_updates["EMBEDDING_PROVIDER"] = payload.embedding_provider

    if payload.embedding_model is not None:
        settings.EMBEDDING_REMOTE_MODEL = payload.embedding_model
        os.environ["EMBEDDING_REMOTE_MODEL"] = payload.embedding_model
        env_updates["EMBEDDING_REMOTE_MODEL"] = payload.embedding_model

    if payload.embeddings_remote_enabled is not None:
        settings.EMBEDDINGS_REMOTE_ENABLED = payload.embeddings_remote_enabled
        os.environ["EMBEDDINGS_REMOTE_ENABLED"] = str(payload.embeddings_remote_enabled).lower()
        env_updates["EMBEDDINGS_REMOTE_ENABLED"] = str(payload.embeddings_remote_enabled).lower()

    if any(k in env_updates for k in ("EMBEDDING_PROVIDER", "EMBEDDING_REMOTE_MODEL", "EMBEDDINGS_REMOTE_ENABLED")):
        embedding_service._remote = None
        embedding_service._remote_checked = False
        embedding_service.warm(block=False)

    if env_updates:
        _persist_env_file(env_updates)

    return await server_config()


#: What schema-only withholds, in the words the UI shows. Kept beside the code
#: that implements it (`prompts.generate_system_context`) so the two cannot drift.
SCHEMA_ONLY_WITHHELD = [
    "Sample rows",
    "Per-column example values",
    "Numeric distributions (count, mean, std, min, max)",
    "Distinct values of categorical columns",
]


def _data_mode_response(session: Session) -> DataModeResponse:
    return DataModeResponse(
        mode=session.data_mode,  # type: ignore[arg-type]
        description=describe_mode(session.data_mode),
        schema_only=session.data_policy.schema_only,
        per_dataset=dict(session.data_policy.per_dataset),
        allowed_providers=sorted(allowed_providers(session.data_mode)),
        # Only meaningful where a prompt can be cloud-bound. Under local-only
        # nothing is withheld because nothing is sent.
        withheld=SCHEMA_ONLY_WITHHELD if session.data_policy.schema_only and session.data_mode != "local-only" else [],
        disabled_tools=disabled_tools(session.data_mode),
    )


@router.get("/api/data-mode", response_model=DataModeResponse)
async def get_data_mode(session: Session = Depends(get_session)) -> DataModeResponse:
    """What this session will and will not send anywhere."""
    return _data_mode_response(session)


@router.post("/api/data-mode", response_model=DataModeResponse, dependencies=[Depends(require_api_key)])
async def set_data_mode(request: DataModeRequest, session: Session = Depends(get_session)) -> DataModeResponse:
    """Switches the mode, and drops any role assignment the new mode forbids.

    Clearing the assignment matters: leaving a cloud provider pinned to a role
    under local-only would mean the next question failed instead of running, and
    the user would have to work out why.
    """
    if request.mode is not None:
        session.set_data_mode(request.mode)
        for role in ("manager", "worker", "vision"):
            assigned = session.models.provider_for(role)
            if assigned and check_provider(session.data_mode, assigned, role):
                setattr(session.models, f"{role}_provider", None)
                setattr(session.models, role, None)

    if request.schema_only is not None:
        session.data_policy.schema_only = request.schema_only

    return _data_mode_response(session)


@router.put("/api/data-mode/dataset/{name}", response_model=DataModeResponse, dependencies=[Depends(require_api_key)])
async def set_dataset_policy(
    name: str, request: DatasetPolicyRequest, session: Session = Depends(get_session)
) -> DataModeResponse:
    """Overrides the session default for one source.

    Sources are not alike: a published reference table and a payroll export do
    not deserve the same answer, and one session-wide setting means picking the
    wrong one for one of them.
    """
    if name not in session.datasets:
        raise HTTPException(status_code=404, detail=f"No dataset named {name!r} in this session")
    session.data_policy.set_for(name, request.schema_only)
    return _data_mode_response(session)


@router.delete(
    "/api/data-mode/dataset/{name}", response_model=DataModeResponse, dependencies=[Depends(require_api_key)]
)
async def clear_dataset_policy(name: str, session: Session = Depends(get_session)) -> DataModeResponse:
    """Drops the override so this source follows the session default again."""
    session.data_policy.clear_for(name)
    return _data_mode_response(session)


def _permissions_response(session: Session) -> PermissionsResponse:
    state = session.permissions
    return PermissionsResponse(
        profile=normalize_profile(state.profile),  # type: ignore[arg-type]
        description=describe_profile(state.profile),
        categories=[
            PermissionCategoryResponse(
                key=category.key,
                label=category.label,
                description=category.description,
                ruling=state.ruling_for(category.key),  # type: ignore[arg-type]
                always_ask=category.always_ask,
                live=category.live,
            )
            for category in CATEGORIES
        ],
        grants=sorted(f"{key}:{subject}" if subject else key for key, subject in state.grants),
    )


@router.get("/api/permissions", response_model=PermissionsResponse)
async def get_permissions(session: Session = Depends(get_session)) -> PermissionsResponse:
    """What this session asks about before acting."""
    return _permissions_response(session)


@router.post("/api/permissions", response_model=PermissionsResponse, dependencies=[Depends(require_api_key)])
async def set_permissions(request: PermissionsRequest, session: Session = Depends(get_session)) -> PermissionsResponse:
    """Sets the profile and any per-category rulings.

    Tightening the profile clears grants already given. A grant is consent for a
    specific thing under the rules in force when it was given; leaving them in
    place would mean choosing a stricter profile changed nothing about what the
    agent was still free to do.
    """
    state = session.permissions
    previous = normalize_profile(state.profile)

    if request.categories:
        for key, ruling in request.categories.items():
            try:
                state.set_ruling(key, ruling)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.profile is not None:
        state.profile = normalize_profile(request.profile)
        if state.profile != previous and previous == "auto-approve":
            state.grants.clear()
            state.extra_roots = ()

    return _permissions_response(session)


@router.get("/api/providers", response_model=ProvidersResponse)
async def list_providers(session: Session = Depends(get_session)) -> ProvidersResponse:
    """Every backend, whether it has a key, and whether this mode allows it.

    Network-free by construction — this renders on every page load.
    """
    return ProvidersResponse(
        providers=[ProviderInfo(**entry) for entry in model_registry.available_providers(session.data_mode)],
        data_mode=session.data_mode,  # type: ignore[arg-type]
    )


@router.put("/api/providers/{provider}/credentials", dependencies=[Depends(require_api_key)])
async def set_provider_credential(
    provider: str,
    request: ProviderCredentialRequest,
    credential_store: CredentialStore = Depends(get_credential_store),
) -> dict:
    """Stores an API key on this machine. The key is never read back."""
    if not provider_exists(provider):
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}")
    if not await asyncio.to_thread(credential_store.set, provider, request.api_key):
        raise HTTPException(status_code=500, detail="Could not write the credentials file. See the server log.")
    # A key changes what a client can reach, and both are cached.
    model_registry.invalidate(provider)
    llm_provider.clear_cache()
    return {"status": "saved", "provider": provider, "key_hint": credential_store.hint(provider)}


@router.delete("/api/providers/{provider}/credentials", dependencies=[Depends(require_api_key)])
async def delete_provider_credential(
    provider: str,
    credential_store: CredentialStore = Depends(get_credential_store),
) -> dict:
    if not provider_exists(provider):
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}")
    removed = await asyncio.to_thread(credential_store.delete, provider)
    model_registry.invalidate(provider)
    llm_provider.clear_cache()
    return {"status": "removed" if removed else "not_stored", "provider": provider}


@router.get("/api/usage", response_model=UsageResponse)
async def session_usage(session: Session = Depends(get_session)) -> UsageResponse:
    """Tokens and, where the price is published, spend for this session.

    ``local_only`` is what lets the client state that nothing was spent instead
    of rendering a zero that looks computed.
    """
    totals = usage_ledger.totals(session.id)
    return UsageResponse(**totals, local_only=session.data_mode == "local-only")


@router.get("/api/models", response_model=ModelListResponse)
async def list_models(
    refresh: bool = False,
    provider: str | None = None,
    session: Session = Depends(get_session),
) -> ModelListResponse:
    """Models installed on one provider, so the user can actually pick one.

    ``provider`` selects which backend to enumerate. Discovery talks to a
    possibly-unreachable host, so it runs off the event loop.
    """
    resolved = settings.resolve_provider(provider)
    models = await asyncio.to_thread(model_registry.list_models, refresh, resolved)
    suggested = await asyncio.to_thread(model_registry.suggest, resolved)

    return ModelListResponse(
        provider=resolved,
        models=[ModelInfoResponse(**model.to_dict()) for model in models],
        suggested=suggested,
        selected={
            # Falls back to what discovery resolved, not to the configured
            # default -- that is empty now, and reporting "" as the selected
            # model would leave the picker showing nothing while the run used
            # something real.
            "manager": session.models.manager or settings.MODEL_NAME or suggested.get("manager"),
            "worker": session.models.worker or settings.WORKER_MODEL_NAME or suggested.get("worker"),
            "vision": session.models.vision or settings.VISION_MODEL_NAME or suggested.get("vision"),
            "temperature": session.models.temperature
            if session.models.temperature is not None
            else settings.TEMPERATURE,
            "manager_provider": session.models.manager_provider or settings.API_PROVIDER,
            "worker_provider": session.models.worker_provider or settings.API_PROVIDER,
            "vision_provider": session.models.vision_provider or settings.API_PROVIDER,
        },
        providers=[ProviderInfo(**entry) for entry in model_registry.available_providers(session.data_mode)],
        error=model_registry.error_for(resolved) if not models else None,
    )


@router.get("/api/models/downloads", response_model=ModelDownloadsResponse)
async def list_downloads(provider: str | None = None) -> ModelDownloadsResponse:
    """In-flight and just-finished installs, plus whether this provider allows them.

    Polled by the client while a download runs. Every download is listed
    regardless of ``provider`` — a pull started on one provider must stay
    visible after the picker is switched to another, or it looks abandoned.
    """
    return ModelDownloadsResponse(
        downloads=[ModelDownloadState(**entry) for entry in model_downloader.list()],
        capability=ProviderDownloadCapability(**model_downloader.capability(provider)),
    )


@router.post(
    "/api/models/download",
    response_model=ModelDownloadState,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
async def download_model(request: ModelDownloadRequest) -> ModelDownloadState:
    """Starts installing a model. Returns immediately; poll ``/api/models/downloads``."""
    try:
        state = model_downloader.start(request.provider, request.model)
    except ProviderNotDownloadable as exc:
        # Not the caller's mistake — the provider or the machine cannot do this.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelDownloadState(**state.to_dict())


@router.post("/api/models/download/cancel", dependencies=[Depends(require_api_key)])
async def cancel_download(request: ModelDownloadRequest) -> dict:
    cancelled = await asyncio.to_thread(model_downloader.cancel, request.provider, request.model)
    return {"status": "cancelling" if cancelled else "not_running"}


@router.delete("/api/models/installed", dependencies=[Depends(require_api_key)])
async def delete_model(model: str, provider: str | None = None, confirm: str | None = None) -> dict:
    """Removes an installed model. Ollama only — LM Studio's CLI has no delete.

    ``confirm`` must repeat ``model`` back exactly. This is a destructive,
    irreversible action reachable by a one-line request, and an API key alone
    only proves the caller is authorized -- not that ``model`` is the one they
    meant to delete rather than one grabbed from a stale link or a typo'd
    query string.
    """
    if confirm != model:
        raise HTTPException(
            status_code=400,
            detail="Pass confirm=<model name>, matching `model` exactly, to delete an installed model.",
        )
    try:
        await asyncio.to_thread(model_downloader.remove, provider, model)
    except ProviderNotDownloadable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface the provider's own words
        raise HTTPException(status_code=502, detail=f"Could not delete {model}: {exc}") from exc
    return {"status": "deleted", "model": model}


@router.post("/api/models", response_model=SessionResponse, dependencies=[Depends(require_api_key)])
async def select_models(selection: ModelSelection, session: Session = Depends(get_session)) -> SessionResponse:
    """Sets this session's preferred models. Unspecified fields keep their value."""
    # Refused here rather than at run time: a 409 naming the mode is actionable,
    # a failed question three clicks later is not.
    for role in ("manager", "worker", "vision"):
        chosen = getattr(selection, f"{role}_provider")
        refusal = check_provider(session.data_mode, chosen, role) if chosen else None
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)

    for role in ("manager", "worker", "vision"):
        model = getattr(selection, role)
        provider = getattr(selection, f"{role}_provider")
        if model is not None:
            setattr(session.models, role, model or None)
        if provider is not None:
            setattr(session.models, f"{role}_provider", provider or None)
            # A provider switch without a model name would otherwise send the
            # previous backend's model id to the new one, and an Ollama tag is a
            # 404 on LM Studio. Resolve a real default from what that provider
            # actually has.
            if model is None:
                suggested = await asyncio.to_thread(model_registry.suggest, provider)
                setattr(session.models, role, suggested.get(role))

    if selection.temperature is not None:
        session.models.temperature = selection.temperature

    # Clients are keyed by spec, so a changed temperature must not reuse a warm client.
    llm_provider.clear_cache()
    return SessionResponse(**session.describe())
