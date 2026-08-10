"""Health, capability discovery and model selection."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_credential_store, get_session, require_api_key
from src.api.schemas import (
    DataModeRequest,
    DataModeResponse,
    DatasetPolicyRequest,
    HealthResponse,
    ModelDownloadRequest,
    ModelDownloadsResponse,
    ModelDownloadState,
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
    UsageResponse,
)
from src.config import settings
from src.core.credentials import CredentialStore
from src.core.data_mode import allowed_providers, check_provider, describe_mode, disabled_tools
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


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    backend = runtime_backend.active_backend()
    return HealthResponse(
        status="ok",
        version=API_VERSION,
        sandbox_available=backend == "docker",
        execution_backend=backend,
        model_provider=settings.API_PROVIDER,
    )


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
        plot_format=settings.PLOT_FORMAT,
        sandbox_available=backend == "docker",
        sandbox_enabled=settings.SANDBOX_ENABLED,
        execution_backend=backend,
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
    )


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
        models=[model.to_dict() for model in models],
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
