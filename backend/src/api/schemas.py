"""Request and response models for the HTTP surface."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import PydanticCustomError

from src.providers import PROVIDERS, exists as provider_exists


def _known_provider(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if not provider_exists(cleaned):
        # A PydanticCustomError, not a bare ValueError: the validation handler
        # serialises `errors()` straight to JSON, and a raw exception in the
        # error context is not serialisable.
        raise PydanticCustomError(
            "unknown_provider",
            "Unknown provider '{value}'. Known providers: {known}",
            {"value": value, "known": ", ".join(PROVIDERS)},
        )
    return cleaned


# Rejecting an unknown provider at the schema boundary means the session can
# never hold a value the LLM layer would silently fall back on. Validated against
# the descriptor table rather than a Literal, so adding a backend stays a row.
ProviderName = Annotated[str, AfterValidator(_known_provider)]

DataModeName = Literal["local-only", "cloud-only", "hybrid"]
PermissionProfileName = Literal["auto-approve", "ask-always", "custom"]
PermissionRulingName = Literal["allow", "ask", "deny"]


class ErrorDetail(BaseModel):
    detail: Any


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    sandbox_available: bool
    execution_backend: str = "inprocess"
    model_provider: str


class ServerConfig(BaseModel):
    """Capabilities the client uses to decide what UI to show."""

    app_name: str
    version: str
    plot_format: Literal["png", "html"]
    sandbox_available: bool
    sandbox_enabled: bool
    model_provider: str
    supported_formats: list[str]
    max_upload_mb: int
    queue_backend: str
    cache_backend: str
    embeddings_semantic: bool
    #: What actually produces vectors: "provider:<model>", "local:<model>" or
    #: "lexical". `embeddings_semantic` alone could not distinguish a provider
    #: embedding model from an in-process one, and the two fail differently.
    embeddings_backend: str = "lexical"
    rag_enabled: bool
    council_enabled: bool
    requires_api_key: bool
    # How the agentic loop is configured. The client shows these read-only —
    # they come from backend/.env and changing one needs a restart.
    agent_tier: str = "auto"
    agent_max_iterations: int = 24
    agent_require_approval: bool = False
    agent_permission_profile: PermissionProfileName = "ask-always"
    agent_consent_timeout: float = 120.0
    agent_verify: bool = True
    agent_grounding_check: bool = True
    context_docs_enabled: bool = True
    supported_document_formats: list[str] = Field(default_factory=list)
    agent_turn_timeout: float = 300.0
    # What local inference was actually configured with. These are derived from
    # the machine unless pinned, and getting them wrong is the usual reason a
    # question is slow — so they are shown rather than left in a file.
    llm_num_thread: int = 0
    llm_num_ctx: int = 0
    llm_keep_alive: str = ""
    #: Whether the manager and worker can be in memory at the same time on this
    #: machine, and what they are estimated to need. This is what decides
    #: between "both stay loaded" and "each is released after it runs", which is
    #: the difference between a 7B pair being usable here and thrashing.
    memory_plan: dict | None = None
    #: Settings that will make this install slow, in plain language. Empty when
    #: there is nothing to say, which is the common case.
    performance_notes: list[str] = Field(default_factory=list)
    # Where generated code runs, and on what machine. `sandbox_available` says
    # only whether Docker answered; these say what is actually in use.
    execution_backend: Literal["host", "docker", "inprocess"] = "inprocess"
    execution_backend_setting: str = "host"
    #: What is actually containing the code — `container`, `os-sandbox`,
    #: `process` or `none`. Distinct from the backend name, because the host
    #: backend's containment depends on what this OS could enforce.
    execution_isolation: str = "none"
    #: `off` / `best-effort` / `require`.
    host_sandbox: str = "off"
    #: Per-feature support on this machine, each with a reason when it is absent.
    #: Renders without running anything; `GET /api/sandbox/selftest` is what
    #: actually proves it.
    sandbox_capability: dict = Field(default_factory=dict)
    #: The configured default. A session may hold a different one.
    data_mode: str = "local-only"
    data_schema_only: bool = True
    sandbox_tier: str = "standard"
    system_profile: str = "server"
    host_cores: int = 0
    host_ram_gb: float | None = None
    sandbox_mem_limit: str = ""
    max_sessions: int = 0


class SessionResponse(BaseModel):
    session_id: str
    created_at: float
    last_seen: float
    has_data: bool
    active_dataset: str | None = None
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    models: dict[str, Any] = Field(default_factory=dict)
    data_mode: str = "local-only"
    data_policy: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    #: True when a real boundary was in force, not merely a separate process.
    #: Derived from what the runtime *reported* enforcing, not from the
    #: backend name alone -- `best-effort` on an old kernel or a Windows box
    #: without pywin32 can silently apply nothing.
    sandboxed: bool = False
    execution_backend: str = "inprocess"
    #: What was actually enforced -- "fully enforced", "partial: +fs -network",
    #: "not started" (no runtime yet, nothing to ask) or "unreported" (Docker,
    #: or HOST_SANDBOX=off, where this question does not apply).
    sandbox_detail: str = "unreported"


class ModelInfoResponse(BaseModel):
    name: str
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    capabilities: list[str] = Field(default_factory=list)
    installed: bool = True
    provider: str = ""
    context_length: int = 0
    loaded: bool | None = None


class ProviderInfo(BaseModel):
    id: str
    label: str = ""
    kind: str = "local"
    base_url: str = ""
    configured: bool = False
    local: bool = False
    is_default: bool = False
    requires_key: bool = False
    #: Whether a key is available at all, from the environment or the store.
    #: Never the key itself — only `key_hint`, which is masked.
    has_key: bool = False
    key_stored: bool = False
    key_hint: str = ""
    #: Whether the session's data mode permits this provider.
    allowed: bool = True
    hint: str = ""
    docs_url: str = ""


class ProviderCredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo] = Field(default_factory=list)
    data_mode: DataModeName = "local-only"


class DataModeRequest(BaseModel):
    mode: DataModeName | None = None
    schema_only: bool | None = None


class DatasetPolicyRequest(BaseModel):
    """Overrides the session default for one source."""

    schema_only: bool


class DataModeResponse(BaseModel):
    mode: DataModeName
    description: str
    schema_only: bool
    #: Per-source overrides. Absent from this map means "follow the default".
    per_dataset: dict[str, bool] = Field(default_factory=dict)
    #: Providers this mode permits, so the picker does not have to re-derive it.
    allowed_providers: list[str] = Field(default_factory=list)
    #: What schema-only actually withholds, in the words the UI shows.
    withheld: list[str] = Field(default_factory=list)
    #: Tools this mode switches off entirely, so the UI can present them as
    #: unavailable rather than letting the user discover it mid-run.
    disabled_tools: list[str] = Field(default_factory=list)


class PermissionsRequest(BaseModel):
    profile: PermissionProfileName | None = None
    #: Per-category rulings, applied only under `custom`. Sent sparsely: a client
    #: flipping one row need not echo the whole matrix back.
    categories: dict[str, PermissionRulingName] | None = None


class PermissionCategoryResponse(BaseModel):
    key: str
    label: str
    description: str
    ruling: PermissionRulingName
    #: Never resolves to allow from the profile alone. The UI renders these as a
    #: higher-friction control rather than one row among equals.
    always_ask: bool = False
    #: Whether anything in the running system reaches this gate yet. False means
    #: the setting is real but inert, and the UI says so instead of implying a
    #: capability that has not shipped.
    live: bool = True


class PermissionsResponse(BaseModel):
    profile: PermissionProfileName
    description: str
    categories: list[PermissionCategoryResponse] = Field(default_factory=list)
    #: Approvals already given this session, so the UI can show what it is no
    #: longer being asked about.
    grants: list[str] = Field(default_factory=list)


class UsageRecordResponse(BaseModel):
    provider: str
    model: str
    role: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    #: None when the model is local or its price is not published. Never 0.0 as
    #: a stand-in for "unknown".
    cost_usd: float | None = None
    estimated: bool = False
    cloud: bool = False


class UsageResponse(BaseModel):
    records: list[UsageRecordResponse] = Field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    any_cloud: bool = False
    estimated: bool = False
    unpriced_models: list[str] = Field(default_factory=list)
    #: True when nothing in this session can incur cost, so the client can say
    #: so rather than render a zero.
    local_only: bool = True


class ModelListResponse(BaseModel):
    provider: str
    models: list[ModelInfoResponse]
    suggested: dict[str, str | None]
    selected: dict[str, Any]
    providers: list[ProviderInfo] = Field(default_factory=list)
    error: str | None = None


class ModelDownloadRequest(BaseModel):
    """A model to install. ``provider`` defaults to the configured one."""

    model: str = Field(min_length=1, max_length=200)
    provider: ProviderName | None = None


class ModelDownloadState(BaseModel):
    provider: str
    model: str
    status: Literal["queued", "downloading", "completed", "failed", "cancelled"]
    completed_bytes: int = 0
    total_bytes: int = 0
    #: None when the provider reports no measurable progress — LM Studio says
    #: nothing at all while it resolves a repo, and a bar stuck at 0% reads as
    #: broken where "Resolving" does not.
    percent: float | None = None
    detail: str = ""
    error: str | None = None
    started_at: float
    finished_at: float | None = None


class ProviderDownloadCapability(BaseModel):
    """Whether models can be installed from here, and the reason when not."""

    provider: str
    can_download: bool = False
    can_delete: bool = False
    reason: str = ""


class ModelDownloadsResponse(BaseModel):
    downloads: list[ModelDownloadState] = Field(default_factory=list)
    capability: ProviderDownloadCapability


class ModelSelection(BaseModel):
    manager: str | None = Field(default=None, max_length=200)
    worker: str | None = Field(default=None, max_length=200)
    vision: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    manager_provider: ProviderName | None = None
    worker_provider: ProviderName | None = None
    vision_provider: ProviderName | None = None


class DatasetSummary(BaseModel):
    name: str
    #: How generated code addresses this table: `tables['<table_key>']`.
    table_key: str = ""
    rows: int
    columns: list[str]
    column_count: int
    source_format: str
    profile: dict[str, Any] = Field(default_factory=dict)
    loaded_at: float
    #: The connection this table was imported from, or `""` for an uploaded file.
    origin: str = ""


class UploadResponse(BaseModel):
    message: str
    dataset: DatasetSummary
    cleaning_result: str
    warnings: list[str] = Field(default_factory=list)
    catalog: dict[str, Any] = Field(default_factory=dict)
    session_id: str


class PreviewResponse(BaseModel):
    page: int
    per_page: int
    total_rows: int
    total_pages: int
    columns: list[str]
    data: list[dict[str, Any]]


# ---------------------------------------------------------------- connections --
# A connection is an ingest source parallel to file upload. The secret half never
# appears in any of these models: a request may carry one inbound, and no response
# type has a field to put one in -- which is stronger than remembering to strip it.


class ConnectorKindResponse(BaseModel):
    kind: str
    label: str
    fields: list[str] = Field(default_factory=list)
    requires_secret: bool = False
    description: str = ""
    #: Whether the driver is importable here. False is not an error -- it is the
    #: reason the UI shows `install_hint` instead of a button that would fail.
    available: bool = True
    install_hint: str = ""


class ConnectionSummary(BaseModel):
    id: str
    name: str
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    created_at: float
    #: Whether a secret is stored, never the secret. The same rule the provider
    #: key routes follow: reading one back to render it would put it in a
    #: response, a browser cache and a devtools log for no benefit.
    has_secret: bool = False
    available: bool = True
    install_hint: str = ""


class ConnectionRequest(BaseModel):
    name: str
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    #: `None` means "leave the stored secret alone", which is what an edit that
    #: did not retype the password must do. `""` means "there is no secret".
    secret: str | None = None


class ConnectionListResponse(BaseModel):
    connections: list[ConnectionSummary] = Field(default_factory=list)
    kinds: list[ConnectorKindResponse] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    ok: bool
    detail: str = ""


class ConnectionColumn(BaseModel):
    name: str
    type: str = ""


class ConnectionTarget(BaseModel):
    name: str
    namespace: str = ""
    qualified: str
    columns: list[ConnectionColumn] = Field(default_factory=list)
    row_estimate: int | None = None


class ConnectionSchemaResponse(BaseModel):
    targets: list[ConnectionTarget] = Field(default_factory=list)


class ConnectionImportRequest(BaseModel):
    target: str
    make_active: bool = True


class ConnectionImportResponse(BaseModel):
    message: str
    dataset: DatasetSummary
    truncated: bool = False
    session_id: str


class ConnectionWriteRequest(BaseModel):
    #: The session dataset to write, by name.
    dataset: str
    #: The table at the far end. Not derived from the dataset name: writing back
    #: to a differently-named table is the ordinary case, not the exception.
    target: str


class WriteBackRequest(BaseModel):
    enable: bool
    #: The connection's own name, typed back. Write-back is described in the spec
    #: as enabled "once, deliberately"; a decision whose blast radius is outside
    #: this machine should cost more than one click.
    confirm: str = ""


#: ``auto`` lets the agent choose its own depth, ``fast`` forces a single shot,
#: ``deep`` forces a full investigation. ``planning`` is the legacy name for
#: "investigate, but let me approve the plan first" and is kept so existing
#: clients and stored sessions keep working.
AnalysisMode = Literal["auto", "fast", "deep", "planning"]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    mode: AnalysisMode = "auto"
    approved_plan: str | None = Field(default=None, max_length=20000)


class ChatResponse(BaseModel):
    response: str
    code: str = ""
    thought: str | None = None
    plan: str | None = None
    image: str | None = None
    status: str = "completed"
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    approval: dict[str, Any] | None = None
    downloads: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    # What the investigation established, and how far it was trusted.
    findings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    iterations: int = 0
    tier: str = "balanced"
    mode: str = "auto"
    verification: str = ""
    grounding: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    #: Which skills informed this turn. Empty is the ordinary case and means
    #: nothing matched, not that the feature is off.
    skills_used: list[str] = Field(default_factory=list)


class SkillSummary(BaseModel):
    name: str
    description: str
    layer: str
    layer_label: str
    path: str
    tags: list[str] = Field(default_factory=list)
    version: str = ""
    chars: int = 0
    chunks: int = 0
    #: False for built-in skills, which live in the checkout and would lose an
    #: edit on the next update. The UI renders the reason rather than a disabled
    #: control with no explanation.
    writable: bool = True
    #: Provenance, present only for a skill installed from a repository. It comes
    #: from the local install index, never from the skill file's own frontmatter —
    #: a fetched file describing its own origin is a claim, not a record.
    source_url: str | None = None
    source_ref: str | None = None
    pinned_sha: str | None = None
    installed_at: float | None = None
    updated_at: float | None = None
    #: Which layer overrides this one, when a more specific layer defines the
    #: same name. Without it, editing the shadowed copy appears to do nothing.
    shadowed_by: str | None = None
    #: How many analyses this skill has informed, and when it last did. The
    #: milestone's browser is asked to show "which analyses used which skill",
    #: and the live `skill` frame cannot answer that — it is gone by the time
    #: anyone opens this page.
    uses: int = 0
    last_used: float | None = None


class SkillDetail(SkillSummary):
    body: str = ""
    #: The recent questions this skill informed, newest first. On the detail view
    #: rather than the list, because it is the answer to "used for what?" and the
    #: list only has room for "how often".
    recent_uses: list[dict[str, Any]] = Field(default_factory=list)


class SkillRoot(BaseModel):
    layer: str
    label: str
    path: str
    writable: bool


class SkillListResponse(BaseModel):
    skills: list[SkillSummary] = Field(default_factory=list)
    roots: list[SkillRoot] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    #: Skills fetched from a repository and awaiting review. They are on disk but
    #: in a directory the registry does not scan, so nothing here is reachable by
    #: the agent — which is the point of listing them separately.
    pending: list[dict[str, Any]] = Field(default_factory=list)
    #: Where installs are fetched from, and whether a token is saved. The token
    #: itself is never returned by any route.
    registry: dict[str, Any] = Field(default_factory=dict)


class SkillInstallRequest(BaseModel):
    """A GitHub repository or gist URL to fetch skills from."""

    url: str = Field(min_length=1, max_length=2048)


class SkillInstallPreviewResponse(BaseModel):
    """What was fetched, pinned, and staged — with nothing yet installed."""

    pending: list[dict[str, Any]] = Field(default_factory=list)
    sha: str = ""
    short_sha: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class SkillPendingListResponse(BaseModel):
    pending: list[dict[str, Any]] = Field(default_factory=list)
    root: str = ""


class SkillUpdateRequest(BaseModel):
    """Whether to apply the update or only report what it would change.

    Defaults to *reporting*. "Pin, don't track" means an installed skill changes
    only when someone says so, and a request that applies by default would make
    the diff a courtesy rather than a step.
    """

    apply: bool = False


class GitHubTokenRequest(BaseModel):
    """A personal access token, or an empty string to remove the stored one."""

    token: str = Field(default="", max_length=512)


class SkillWriteRequest(BaseModel):
    name: str
    description: str
    body: str
    tags: list[str] = Field(default_factory=list)
    #: Set when this skill is being promoted from a recurring analysis, so the
    #: candidate can be marked and stop being offered.
    candidate_id: int | None = None


class SkillDraftRequest(BaseModel):
    """Asks for a skill draft for an analysis the user is looking at.

    The other promotion path. `GET /candidates/{id}/draft` serves an offer the
    backend made after a threshold; this serves one the *user* initiated about a
    completed analysis, which the milestone lists separately and which does not
    wait for a count.
    """

    instruction: str = Field(min_length=1, max_length=4000)


class SkillCandidateListResponse(BaseModel):
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    threshold: int = 3


class DocumentSummary(BaseModel):
    name: str
    chars: int
    chunks: int
    source_format: str
    preview: str = ""


class DocumentUploadResponse(BaseModel):
    message: str
    document: DocumentSummary
    session_id: str


class WorkspaceFile(BaseModel):
    name: str
    path: str
    size: int
    type: str
    modified_at: float


class WorkspaceListing(BaseModel):
    files: list[WorkspaceFile]


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    result: Any = None


class ReportResponse(BaseModel):
    report: str
    interaction_count: int


class VariablesResponse(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    sandbox_available: bool = False


class SandboxSelfTestResponse(BaseModel):
    """What a probe child was actually prevented from doing."""

    ok: bool = False
    detail: str = ""
    #: One entry per attempt: `blocked` / `allowed` / `inconclusive`, with a
    #: reason. Three outcomes rather than two — "nothing stopped it" is not the
    #: same claim as "it would have worked".
    checks: dict[str, Any] = Field(default_factory=dict)
    #: What the child reported applying to itself, read back from the runtime.
    applied: dict[str, Any] = Field(default_factory=dict)
    capability: dict[str, Any] = Field(default_factory=dict)
