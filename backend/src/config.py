from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Both sit outside `core/` because `Settings` is built at import time and
# `core.*.__init__` imports `settings` back.
from src.providers import CLOUD_PROVIDERS, LOCAL_PROVIDERS, PROVIDERS, describe, is_cloud
from src.utils.hostinfo import host_info
from src.utils.logging import logger


# Not a Literal any more: the set of backends is data in `src.providers`, and
# `_validate_provider` below still rejects an unknown name at boot.
Provider = str

# Re-exported for callers that have always imported them from here.
__all__ = [
    "CLOUD_PROVIDERS",
    "LOCAL_PROVIDERS",
    "PROVIDERS",
    "AgentTier",
    "DataMode",
    "Provider",
    "TierBudget",
    "settings",
]


# What the user has agreed may leave this machine, and the mechanism behind the
# local-first promise: `local-only` refuses a cloud provider outright rather than
# quietly using it, `cloud-only` is the inverse, `hybrid` assigns per role.
DataMode = Literal["local-only", "cloud-only", "hybrid"]


# How the agentic loop is sized for the model actually behind it.
#
# The loop asks a model to choose its next action from real execution output.
# A frontier model does that well and is worth giving a long leash; a 1.5B local
# model does it badly and every extra iteration is another chance to wander, so
# it gets a short budget, a smaller action menu and deterministic fallbacks.
# One codebase, three shapes -- rather than three code paths.
AgentTier = Literal["compact", "balanced", "full"]


@dataclass(frozen=True)
class TierBudget:
    """Per-tier limits for one analysis turn."""

    #: Which tier produced this budget. Carried on the object so callers can
    #: report it without reverse-matching the numbers back to a tier.
    tier: str
    #: Iterations allowed in `auto` mode before the agent must answer.
    iterations: int
    #: Iterations allowed when the user explicitly asks for a deep run.
    deep_iterations: int
    #: Columns of schema detail spent per prompt.
    max_columns: int
    #: Retrieved context-document chunks injected per decision.
    doc_chunks: int
    #: Whether the agent may spend a whole iteration on reflection alone.
    #: Small models reliably waste it restating the question.
    allow_reflection: bool
    #: Whether verification re-derives the result with a second execution.
    allow_verification: bool
    #: Characters of prior-step output carried into the next decision.
    observation_chars: int
    #: Whether the *model* chooses the next action, or the loop does.
    #:
    #: Asking costs a manager round-trip per iteration, and on a small model it
    #: buys nothing: it reads a 1500-character transcript and picks from three
    #: options it does not reliably distinguish. Worse, a reasoning distill
    #: spends its whole output budget deliberating and returns nothing usable,
    #: so the round-trip is paid and the default is taken anyway. Below the
    #: balanced tier the loop is therefore deterministic -- run the code, correct
    #: it if it fails, answer -- which is the shape a compact model executes well
    #: and turns a nine-call turn into a three-call one.
    allow_decisions: bool = True
    #: Whether the model may fan a step out into isolated parallel subagents.
    #: Off below balanced for the same reason `allow_decisions` is: choosing to
    #: parallelize is itself a round-trip, and a compact model doesn't steer
    #: its own loop reliably enough to be trusted with a second, nested one.
    allow_subagents: bool = False
    #: Ceiling on how many sub-questions one `parallel` action may spawn.
    max_subagents: int = 0


TIER_BUDGETS: dict[str, TierBudget] = {
    "compact": TierBudget(
        tier="compact",
        iterations=3,
        deep_iterations=5,
        max_columns=25,
        doc_chunks=2,
        allow_reflection=False,
        allow_verification=False,
        observation_chars=1500,
        allow_decisions=False,
    ),
    "balanced": TierBudget(
        tier="balanced",
        iterations=8,
        deep_iterations=14,
        max_columns=60,
        doc_chunks=4,
        allow_reflection=True,
        allow_verification=True,
        observation_chars=4000,
        allow_subagents=True,
        max_subagents=2,
    ),
    "full": TierBudget(
        tier="full",
        iterations=12,
        deep_iterations=24,
        max_columns=120,
        doc_chunks=6,
        allow_reflection=True,
        allow_verification=True,
        observation_chars=8000,
        allow_subagents=True,
        max_subagents=3,
    ),
}

# Below this many billions of parameters a model cannot be trusted to steer its
# own multi-step investigation. The boundary is drawn between the 3B and 7B
# classes because that is where instruction-following on structured action
# selection becomes reliable enough to be worth the round-trip.
COMPACT_MAX_PARAMS_B = 4.0
FULL_MIN_PARAMS_B = 30.0


#: Docker's memory-limit suffixes, which are what `SANDBOX_MEM_LIMIT` speaks.
_MEMORY_UNITS: dict[str, int] = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory(value: str | int | None) -> int | None:
    """``"2g"`` -> bytes. Returns ``None`` for anything unreadable.

    Total by design: this parses a hand-edited .env value, and a typo there
    should cost a default rather than a failed boot.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip().lower().rstrip("ib")  # accepts "2gib" and "2gb"
    if not text:
        return None
    unit = _MEMORY_UNITS.get(text[-1:], None)
    number = text[:-1] if unit else text
    try:
        parsed = float(number)
    except ValueError:
        return None
    return int(parsed * (unit or 1)) or None


def format_memory(num_bytes: int) -> str:
    """Bytes -> a readable Docker suffix form (``"2g"``, ``"512m"``).

    Rounded *down* to a whole gigabyte, or to 256 MB below that. A derived limit
    is an approximation of what the machine can spare, and printing it as
    ``2057637k`` reads like a measurement it is not -- someone comparing it
    against their .env should see a number they could have typed.
    """
    gigabyte = 1024**3
    if num_bytes >= gigabyte:
        return f"{num_bytes // gigabyte}g"
    step = 256 * 1024**2
    return f"{max(1, (num_bytes // step) * step // (1024**2))}m"


def tier_for_parameter_size(parameter_size: str | None) -> AgentTier:
    """Maps a reported parameter count ("1.5B", "7B", "70B") onto a tier.

    Returns ``"balanced"`` for anything unparseable, which is every hosted
    gateway model -- they do not report a size and are not small.
    """
    if not parameter_size:
        return "balanced"
    cleaned = str(parameter_size).strip().upper().rstrip("B")
    try:
        billions = float(cleaned)
    except ValueError:
        return "balanced"
    if billions <= 0:
        return "balanced"
    if billions < COMPACT_MAX_PARAMS_B:
        return "compact"
    if billions >= FULL_MIN_PARAMS_B:
        return "full"
    return "balanced"


class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "Wizard AI Agent"
    ENV: Literal["dev", "prod", "test"] = "dev"
    BASE_DIR: Path = Path(__file__).parent.parent

    # Adaptive hardware profile. "auto" measures the host at boot; the three
    # named profiles pin it. This was previously read by nothing at all, so
    # every resource default was a server's regardless of what it said.
    SYSTEM_PROFILE: Literal["auto", "laptop", "server", "hpc"] = "auto"

    # Model Configuration
    # NOTE: MODEL_TYPE is retained only so existing .env files keep validating.
    # API_PROVIDER is the value the runtime actually branches on, and it is only
    # the *default*: a session may pick a different provider per role.
    #
    # The three role models default to EMPTY, which means "use whatever this
    # provider actually has installed", resolved through `model_registry`.
    # Naming a model here pins it as an override. They were previously hardcoded
    # to specific Ollama tags, which made those two models load-bearing: the tag
    # is a 404 on LM Studio or any gateway, and a fresh install with different
    # models pulled would fail on the first request with an opaque error.
    MODEL_TYPE: Provider = "ollama"
    MODEL_NAME: str = ""
    WORKER_MODEL_NAME: str = ""
    VISION_MODEL_NAME: str = ""
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    FEEDBACK_FILE: str = "feedback_data.json"

    #: Empty means "derive it" — see the `data_mode` property.
    DATA_MODE: str = ""
    #: Cloud-bound prompts carry column names, dtypes and null rates but no real
    #: values. On by default: the conservative option should need no decision.
    DATA_SCHEMA_ONLY: bool = True

    # URL and key fields named by the rows in `src.providers`. Keys may also come
    # from the local credential store — see `provider_api_key`.
    OPENAI_BASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    ANTHROPIC_API_KEY: str = ""

    # ------------------------------------------------------------------ #
    # Embeddings
    #
    # These used to come from `sentence-transformers`, which depends on torch --
    # and on Linux/x86_64 torch installs eleven NVIDIA CUDA wheels unconditionally,
    # whether or not a GPU exists. That is ~2.8 GB of compressed wheels to run a
    # 90 MB MiniLM model, and it was by far the largest thing in the API image.
    #
    # The model server this app already talks to can embed: Ollama exposes
    # POST /api/embed, and every OpenAI-compatible server exposes /v1/embeddings.
    # Using it costs nothing on disk and follows whichever provider is selected.
    # Resolution order is remote -> local sentence-transformers (only if the user
    # installed it) -> the built-in hashing encoder, which always works offline.
    # ------------------------------------------------------------------ #
    #: Keep-alive for a model that cannot share memory with the other role.
    #: Short on purpose: it must expire while the *other* model is working, so
    #: its memory is released before that one needs it. One deliberate reload per
    #: role change is bounded; two oversized models competing for RAM is not.
    LLM_KEEP_ALIVE_SWAP: str = "30s"
    #: Share of system RAM the model server may be planned against. The rest is
    #: the OS, this backend, the sandbox and the user's desktop. 0 means "use the
    #: built-in default"; raise it on a machine that does nothing else.
    MODEL_MEMORY_FRACTION: float = 0.0

    EMBEDDINGS_REMOTE_ENABLED: bool = True
    #: Which provider to embed against. Empty follows API_PROVIDER.
    EMBEDDING_PROVIDER: str = ""
    #: Remote embedding model id. Empty means "discover one from this provider",
    #: which is right because the name differs per backend and per install.
    EMBEDDING_REMOTE_MODEL: str = ""
    EMBEDDING_TIMEOUT: float = 20.0
    #: Timeout for the *first* call only, which is a different operation: the
    #: server has to read the model off disk before it can embed anything. On a
    #: laptop with a cold page cache that took over 20s while every subsequent
    #: encode took 0.05s -- so the steady-state timeout rejected an encoder that
    #: works, and the install silently fell back to lexical retrieval for good.
    #: It is affordable precisely because warm-up no longer runs inside a turn.
    EMBEDDING_COLD_TIMEOUT: float = 180.0
    #: Resolve the encoder in the background at startup rather than inside the
    #: first question. Turning this off restores lazy resolution, where the cost
    #: of a cold model load is paid by whoever asks first.
    EMBEDDINGS_WARM_ON_STARTUP: bool = True
    #: Local sentence-transformers model, used only when that optional package
    #: is installed. Unchanged so existing .env files keep their meaning.
    EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"
    #: Download it if it is not already on disk. Off by default: a 90 MB fetch is
    #: not something a question should trigger, and the provider path is better
    #: anyway. It stays available for deliberately offline installs.
    EMBEDDING_ALLOW_DOWNLOAD: bool = False

    # LM Studio. Stored as a bare root because two API surfaces hang off it:
    # `/v1` (OpenAI-compatible, used for inference) and `/api/v0` (native, used
    # for discovery -- it reports real capabilities and load state).
    # LM Studio binds loopback only by default; enable "Serve on Local Network"
    # for the backend container to reach the host.
    LMSTUDIO_BASE_URL: str = "http://host.docker.internal:1234"
    LMSTUDIO_API_KEY: str = ""  # LM Studio ignores it; present for proxies that don't

    # ------------------------------------------------------------------ #
    # Where generated code runs
    #
    # Three values, and Docker is no longer one of the defaults:
    #
    #   host       one *subprocess* per session running the daemon over a
    #              loopback socket. No image, no daemon to install, and a
    #              runaway allocation or a segfault takes down the child rather
    #              than the API. This is what a fresh clone gets.
    #   docker     one container per session. Opt-in, and unchanged.
    #   inprocess  guarded `exec` inside the API process. No isolation at all.
    #              Kept for CI and for environments where spawning is blocked.
    #
    # `auto` and `local` are the previous spellings and are folded to `host` by
    # `_fold_execution_backend`, so the rest of the codebase only ever sees the
    # three above.
    # ------------------------------------------------------------------ #
    EXECUTION_BACKEND: Literal["host", "docker", "inprocess"] = "host"
    #: Seconds to wait for a freshly spawned host runtime to accept connections.
    #: It imports pandas and matplotlib first, which is seconds on a slow disk.
    HOST_RUNTIME_START_TIMEOUT: float = Field(
        default=60.0, validation_alias=AliasChoices("HOST_RUNTIME_START_TIMEOUT", "LOCAL_RUNTIME_START_TIMEOUT")
    )
    #: Address-space ceiling for a host runtime, mirroring SANDBOX_MEM_LIMIT.
    HOST_RUNTIME_MEM_LIMIT: str = Field(
        default="", validation_alias=AliasChoices("HOST_RUNTIME_MEM_LIMIT", "LOCAL_RUNTIME_MEM_LIMIT")
    )
    #: Whether a host runtime may pip-install a missing package on demand.
    #: Off by default: unlike a container, it would be installing into the
    #: environment the backend itself runs in.
    HOST_RUNTIME_ALLOW_PIP: bool = Field(
        default=False, validation_alias=AliasChoices("HOST_RUNTIME_ALLOW_PIP", "LOCAL_RUNTIME_ALLOW_PIP")
    )

    # ------------------------------------------------------------------ #
    # OS-level containment for the host runtime
    #
    #   off          spawn the child with no OS policy applied.
    #   best-effort  apply whatever this kernel supports and report the rest.
    #   require      refuse to start a runtime that cannot be contained.
    #
    # Three states rather than a bool because a silent downgrade and a refusal
    # are both wrong as a universal answer: a 5.10 kernel has no Landlock and
    # must still be able to run, while someone who chose this setting to get a
    # boundary should not be given a subprocess that merely looks like one.
    # ------------------------------------------------------------------ #
    HOST_SANDBOX: Literal["off", "best-effort", "require"] = "best-effort"
    #: Outbound network for generated code. Loopback is always permitted -- the
    #: daemon protocol is a loopback socket -- so this governs everything else.
    HOST_SANDBOX_NETWORK: Literal["deny", "allow"] = "deny"

    # Sandbox
    #
    # How much of the analysis toolkit the image carries. The libraries are no
    # longer declared to the model from a hand-maintained list -- the runtime is
    # asked what it actually has -- so a smaller image simply advertises less
    # rather than promising something that then fails to import.
    #
    #   core      pandas, numpy, pyarrow, matplotlib, duckdb, openpyxl
    #   standard  + scipy, statsmodels, scikit-learn, xgboost-cpu, lightgbm,
    #             plotly, seaborn, networkx, pillow, xlsxwriter, tabulate
    #   full      + lifelines, geopandas, shapely
    SANDBOX_TIER: Literal["core", "standard", "full"] = "standard"
    #: Overrides the image tag. Empty derives it from the tier, so switching
    #: tiers cannot silently reuse an image built with different libraries.
    SANDBOX_IMAGE: str = ""
    SANDBOX_NETWORK_DISABLED: bool = False
    SANDBOX_DOCKER_RUNTIME: str = ""
    SANDBOX_MEM_LIMIT: str = "2g"
    SANDBOX_CPU_QUOTA: int = 0  # 0 = unlimited; 100000 == 1 CPU
    SANDBOX_PIDS_LIMIT: int = 256
    SANDBOX_EXEC_TIMEOUT: int = 180  # seconds per code execution
    SANDBOX_ALLOW_RUNTIME_PIP: bool = True
    # When False the sandbox container is never created (unit tests / CI / no Docker host).
    SANDBOX_ENABLED: bool = True

    # Enterprise / Cloud API Provider Config
    API_PROVIDER: Provider = "ollama"
    GATEWAY_API_URL: str = ""
    GATEWAY_API_KEY: str = ""
    PLOT_FORMAT: Literal["png", "html"] = "html"

    # ------------------------------------------------------------------ #
    # Inference
    #
    # MAX_TOKENS is a *ceiling*, not a target -- but it used to be the only
    # number, so every call was allowed 4096 tokens of output regardless of what
    # it was for. That is free when a model stops on its own and ruinous when it
    # does not: a reasoning distill asked to pick one word from a three-item menu
    # will happily spend the entire budget deliberating, which on a CPU-bound
    # 1.5B model is four minutes for a decision worth sixty tokens. The per-call
    # budgets below are what each kind of call actually needs.
    # ------------------------------------------------------------------ #
    MAX_TOKENS: int = 4096
    TEMPERATURE: float = 0.0
    #: Context window requested from Ollama. Derived from the host when unset --
    #: this is a *load-time* parameter, so it fixes the KV cache Ollama allocates
    #: for every resident model, and two models at 16k on a 16 GB laptop is the
    #: difference between both staying warm and one being evicted on every
    #: manager/worker alternation. Not sent to OpenAI-compatible servers, which
    #: fix context length when the model is loaded.
    LLM_NUM_CTX: int = 0
    #: Inference threads. Derived from physical cores when unset.
    LLM_NUM_THREAD: int = 0
    LLM_REQUEST_TIMEOUT: int = 300
    #: How long a provider should keep a model resident after answering. The
    #: manager and worker alternate all turn, so an eviction between them costs a
    #: full reload from disk on every single iteration. Ollama's own default is
    #: five minutes, which a slow turn can exceed while it is still running.
    LLM_KEEP_ALIVE: str = "30m"

    #: Resolve the manager and worker clients now, on a background thread, so no
    #: question pays for a cold model load. Measured live: two small Ollama models
    #: (qwen2.5:3b, qwen2.5-coder:1.5b) each took ~44s to load from disk during the
    #: first turn of a session -- the dominant cost of that turn, not reasoning
    #: time or call count. Off restores lazy resolution, where whichever role a
    #: question reaches first pays for the load. Cloud/gateway providers are never
    #: warmed regardless of this setting -- see LOCAL_PROVIDERS.
    LLM_WARM_ON_STARTUP: bool = True

    #: Actively unload a role's model at a phase boundary where its idleness is
    #: known in advance (e.g. the manager during a compact-tier investigation
    #: loop -- see `orchestrator.run`), rather than waiting for keep_alive to
    #: expire or Ollama's own LRU to guess. Ollama only -- see
    #: `LLMProvider.release`. Off restores today's purely reactive behavior.
    LLM_RELEASE_IDLE_MODELS: bool = True

    #: Output budget per kind of call. Generous enough that a reasoning model can
    #: finish a thought, small enough that it cannot spend a turn on one.
    LLM_MAX_TOKENS_PLAN: int = 1024
    LLM_MAX_TOKENS_DECISION: int = 512
    LLM_MAX_TOKENS_CODE: int = 1536
    LLM_MAX_TOKENS_ANSWER: int = 1024
    LLM_MAX_TOKENS_REVIEW: int = 256

    MAX_CORRECTION_RETRIES: int = 3

    # ------------------------------------------------------------------ #
    # Agentic loop
    #
    # The analysis is an observe -> decide -> act loop, not a fixed pipeline:
    # the agent sees real execution output and revises what it does next. That
    # is the only shape that answers questions needing several dependent steps,
    # but it costs one manager round-trip per iteration -- so every budget here
    # is scaled by the tier, which is what lets the same code run on a 1.5B
    # local model and on a frontier gateway.
    # ------------------------------------------------------------------ #
    AGENT_TIER: Literal["auto", "compact", "balanced", "full"] = "auto"
    # Hard ceiling regardless of tier or mode. A runaway loop on a paid gateway
    # is a billing incident, so this is deliberately not derived.
    AGENT_MAX_ITERATIONS: int = 24
    # How much of one execution's stdout is fed back into the next decision.
    # The old pipeline passed 200 characters between steps, which silently threw
    # away every intermediate result a later step depended on.
    AGENT_OBSERVATION_CHARS: int = 4000
    # Halt for plan approval before anything runs. Off by default: an agent that
    # asks permission for every question is not autonomous. This gates the
    # *plan*; AGENT_PERMISSION_PROFILE gates actions within an approved one.
    AGENT_REQUIRE_APPROVAL: bool = False
    # How much the agent asks before acting, orthogonal to depth. Defaults to
    # ask-always because every category is then at least as consultative as it
    # was before the profile existed -- auto-approve would silently stop asking
    # about web search, which is a trust regression to ship by default.
    AGENT_PERMISSION_PROFILE: Literal["auto-approve", "ask-always", "custom"] = "ask-always"
    # How long a mid-run consent prompt waits before it counts as declined.
    # A suspended turn nobody answers must end, not park.
    AGENT_CONSENT_TIMEOUT: float = 120.0
    # Re-derive the headline result a second way before answering.
    AGENT_VERIFY: bool = True
    # Refuse to present numbers that never appeared in real execution output.
    AGENT_GROUNDING_CHECK: bool = True
    # Emit a reproducible standalone script for each completed analysis.
    AGENT_EMIT_SCRIPT: bool = True
    # Wall-clock ceiling for one turn, in seconds. When it is reached the loop
    # stops starting new work and answers from what it already has, so a slow
    # model degrades into a worse answer rather than into no answer at all.
    # `0` disables it. This is a deadline, not a kill: whatever call is in
    # flight finishes, because cancelling it would throw away work already paid
    # for and leave the provider mid-generation.
    AGENT_TURN_TIMEOUT: float = 300.0

    # ------------------------------------------------------------------ #
    # Subagents (Milestone 7)
    #
    # A `parallel` decision fans a step out into isolated child loops, each
    # with its own process/container (the daemon protocol is single-in-flight,
    # so real concurrency needs one runtime per branch, not multiplexed calls).
    # Deliberately deterministic and small: no decision or verification
    # round-trip per branch, and a cap independent of the parent's own tier.
    # ------------------------------------------------------------------ #
    SUBAGENT_ENABLED: bool = True
    #: Floored to 1 by a validator below -- a subagent that starts and is
    #: handed zero iterations completes zero steps and folds back nothing,
    #: which fails silently rather than either running or refusing to.
    SUBAGENT_MAX_ITERATIONS: int = 3
    # Wall clock for the whole fan-out, clamped to whatever's left of
    # AGENT_TURN_TIMEOUT when that's set. A branch that hasn't finished by
    # then contributes nothing rather than holding up the parent's own answer.
    SUBAGENT_TIMEOUT: float = 120.0

    # Council review (each specialist costs an LLM round-trip)
    COUNCIL_ENABLED: bool = True
    COUNCIL_TIMEOUT: float = 20.0
    VISION_ENABLED: bool = False

    # ------------------------------------------------------------------ #
    # Context documents
    #
    # Hard analytical questions are rarely answerable from the tables alone --
    # they turn on a data dictionary, a fee schedule, a metric definition. These
    # are ingested alongside the datasets, chunked, and retrieved during a run.
    # ------------------------------------------------------------------ #
    CONTEXT_DOCS_ENABLED: bool = True
    CONTEXT_DOC_MAX_BYTES: int = 32 * 1024 * 1024
    CONTEXT_CHUNK_CHARS: int = 1200
    CONTEXT_CHUNK_OVERLAP: int = 150
    CONTEXT_TOP_K: int = 5

    # ------------------------------------------------------------------ #
    # Skills (Milestone 5)
    #
    # Reusable know-how in the SKILL.md convention, layered built-in /
    # user-global / project-local. Retrieved through the same path context
    # documents use, so it degrades to lexical matching with no encoder loaded.
    #
    # Both directory settings are empty by default, meaning "derive it": the
    # built-in root is found relative to this checkout, and the project root is
    # `.wizard/skills` under the working directory. The user root has no setting
    # at all -- `registry.user_root()` derives it from `utils.appdirs.config_dir()`,
    # so one machine's layout cannot be copied onto every machine through
    # `.env.example`. The test suite pins both of these, so no test reads a
    # developer's own skills or the shipped ones by accident.
    # ------------------------------------------------------------------ #
    SKILLS_ENABLED: bool = True
    SKILLS_BUILTIN_DIR: str = ""
    SKILLS_PROJECT_DIR: str = ""
    # How many skills may inform one plan. Two, because a third is nearly always
    # a worse match than the first two and it competes for the same budget.
    SKILLS_TOP_K: int = 2
    # The whole `<skills>` block's ceiling, not per skill. This is the only place
    # a skill costs prompt budget, and it is spent on the planning prompt alone --
    # the worker prompt is rebuilt per iteration *and* per correction retry, so a
    # block there would be paid for N times per turn.
    SKILLS_MAX_CHARS: int = 1800
    # One floor for both scorers, which is only possible because the fallback is
    # question-coverage rather than the hashing encoder's cosine -- see
    # `SkillRegistry.search` for why that substitution is made.
    SKILLS_MIN_SIMILARITY: float = 0.35
    # How many times an analysis must recur before it is offered for promotion
    # into a named skill. Two is noise -- asking the same question twice in a
    # session is ordinary -- and four means a week of work before the offer
    # appears. Nothing is written until the user confirms.
    SKILL_PROMOTION_THRESHOLD: int = 3

    # ------------------------------------------------------------------ #
    # Installing a skill from GitHub (Milestone 6)
    #
    # A setting rather than a constant so GitHub Enterprise is a configuration
    # change instead of a fork -- it is the same API on a different hostname,
    # which is the one case `parse_source` cannot recognise on its own because
    # only the operator knows the name. The test suite pins it to a refused port,
    # so an un-stubbed fetch fails instantly instead of reaching github.com.
    # ------------------------------------------------------------------ #
    SKILLS_REGISTRY_API: str = "https://api.github.com"
    # Every fetch is bounded. `everything degrades, nothing hangs` is a hard rule
    # and a skill install is the first thing in this codebase that waits on a
    # host nobody in the project controls.
    SKILLS_FETCH_TIMEOUT: float = 20.0
    # One SKILL.md. Generous for instruction text and far below the ~1 MB point
    # where the Contents API stops inlining content at all.
    SKILLS_FETCH_MAX_BYTES: int = 256 * 1024
    # Entries in one directory listing. A repository pointed at by mistake is
    # refused by count rather than discovered one request at a time.
    SKILLS_FETCH_MAX_FILES: int = 200

    # Ingestion limits
    MAX_UPLOAD_BYTES: int = 512 * 1024 * 1024  # 512MB on disk
    MAX_INMEMORY_ROWS: int = 2_000_000
    PROFILE_SAMPLE_ROWS: int = 200_000  # rows used for profiling/catalog on big data
    PROMPT_MAX_COLUMNS: int = 60  # wide-frame guard for prompt context

    # Connections (Milestone 4). An upload is bounded by MAX_UPLOAD_BYTES before
    # anything reads it; a table is not, and `Session._materialize` writes each
    # frame up to three times. Without a ceiling the first honest question asked
    # of a real warehouse is an OOM in the API process -- which is not sandboxed.
    # Truncation is reported through the dataset profile, never silent.
    CONNECTOR_MAX_ROWS: int = 1_000_000
    # An object store has no server-side row limit -- the smallest thing it will
    # return is the whole object -- so CONNECTOR_MAX_ROWS cannot help: it applies
    # after the bytes are already in memory. This is checked against the object's
    # reported size *before* the read, which is the only point where refusing is
    # still cheap. Mirrors MAX_UPLOAD_BYTES, which does the same job for a file.
    CONNECTOR_MAX_OBJECT_BYTES: int = 512 * 1024 * 1024
    # How long a connector may spend reaching a source. The drivers default to
    # 30s or more, which is long enough that an unreachable host reads as a hang
    # rather than as a wrong hostname.
    CONNECTOR_TIMEOUT: int = 10

    # Sessions
    SESSION_TTL_SECONDS: int = 60 * 60 * 6
    SESSION_MAX_ACTIVE: int = 32
    SESSION_HISTORY_TURNS: int = 8

    # Retrieval / RAG
    # Skips the sentence-transformers download entirely and uses the hashing
    # encoder. Needed for air-gapped installs and for CI, where a per-run model
    # download is both slow and a network-flakiness dependency.
    EMBEDDINGS_FORCE_FALLBACK: bool = False
    RAG_ENABLED: bool = True
    RAG_TOP_K: int = 4
    RAG_MIN_SIMILARITY: float = 0.35
    SEMANTIC_CACHE_THRESHOLD: float = 0.92
    TRAJECTORY_MIN_SIMILARITY: float = 0.90

    # Queue / cache backends. Redis is entirely optional: when REDIS_URL is empty
    # (or the redis package is missing) an in-process implementation is used.
    REDIS_URL: str = ""
    QUEUE_MAX_WORKERS: int = 2
    JOB_RESULT_TTL_SECONDS: int = 3600

    # HTTP / transport security
    CORS_ALLOW_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    API_KEY: str = ""  # when set, every mutating route requires X-API-Key
    RATE_LIMIT_MAX_REQUESTS: int = 60
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    WS_MAX_CONCURRENT_PER_IP: int = 4
    #: Comma-separated peer IPs allowed to set X-Forwarded-For. Empty means
    #: nobody is trusted, so the limiter keys on the socket peer alone -- the
    #: header is otherwise self-reported and a client behind no real proxy
    #: could claim any address to evade or collide someone else's bucket.
    FORWARDED_ALLOW_IPS: str = ""
    #: Multiplies RATE_LIMIT_MAX_REQUESTS / WS_MAX_CONCURRENT_PER_IP into a
    #: second, coarser ceiling keyed on the raw address alone. The per-session
    #: limiter gives every session behind a shared address (NAT, reverse
    #: proxy) its own fair budget, but a session id is client-supplied --
    #: without a ceiling on the address too, an attacker could mint a fresh
    #: one per request and multiply their effective rate without bound. Both
    #: ceilings must pass.
    RATE_LIMIT_IP_BURST_MULTIPLIER: int = 8
    #: Ceiling on a JSON request body -- POST /api/chat, /api/connections,
    #: PUT /api/permissions and friends. File upload routes are exempt: they
    #: stream to disk under their own MAX_UPLOAD_BYTES / CONTEXT_DOC_MAX_BYTES
    #: ceiling rather than buffering into a parsed object first, which a JSON
    #: body always does.
    MAX_JSON_BODY_BYTES: int = 2 * 1024 * 1024

    # Paths
    DATA_DIR: Path = Field(default_factory=lambda: Path(__file__).parent.parent / "data")
    LOG_DIR: Path = Field(default_factory=lambda: Path(__file__).parent.parent / "logs")
    WORKSPACE_DIR: Path = Field(default_factory=lambda: Path(__file__).parent.parent.parent / "workspace")

    # A blank env var (`CONNECTOR_MAX_OBJECT_BYTES=`, the shape docker-compose's
    # `${VAR:-}` produces for an unset optional knob) must fall back to the
    # field default rather than fail int/float parsing at import time -- the
    # same "blank counts as unset" rule the host-sizing validator already
    # applies, just enforced before validation instead of inside one field.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    @field_validator("CORS_ALLOW_ORIGINS")
    @classmethod
    def _strip_origins(cls, value: str) -> str:
        return value.strip()

    @field_validator("EXECUTION_BACKEND", mode="before")
    @classmethod
    def _fold_execution_backend(cls, value: str) -> str:
        """Folds the pre-w2 spellings so an existing .env keeps working.

        ``auto`` used to mean "Docker if you have it, subprocess otherwise" and
        ``local`` named the subprocess. Docker is opt-in now, so both resolve to
        ``host`` -- and because this runs *before* the Literal, nothing
        downstream ever has to know the older names existed.
        """
        cleaned = (value or "").strip().lower()
        return "host" if cleaned in ("auto", "local") else cleaned

    @field_validator("SUBAGENT_MAX_ITERATIONS")
    @classmethod
    def _floor_subagent_iterations(cls, value: int) -> int:
        """A misconfigured 0 would give every branch a zero-iteration budget.

        `_act_parallel` takes `min(SUBAGENT_MAX_ITERATIONS, remaining)` for
        the child budget -- a 0 here forces that to 0 regardless of what the
        parent has left, so every subagent starts, runs no steps, and folds
        back nothing. That is silent data loss, not a refusal, so it is
        floored here rather than left to reach the loop at all.
        """
        return max(1, value)

    @field_validator("API_PROVIDER", "MODEL_TYPE")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in PROVIDERS:
            raise ValueError(f"Unknown provider {value!r}. Known providers: {', '.join(PROVIDERS)}")
        return cleaned

    @field_validator("DATA_MODE")
    @classmethod
    def _validate_data_mode(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned and cleaned not in ("local-only", "cloud-only", "hybrid"):
            raise ValueError(f"Unknown DATA_MODE {value!r}. Use local-only, cloud-only or hybrid.")
        return cleaned

    @field_validator("LMSTUDIO_BASE_URL")
    @classmethod
    def _normalize_lmstudio_url(cls, value: str) -> str:
        # LM Studio's own UI displays the endpoint as ".../v1", so that is what
        # people paste. Discovery needs the root, so accept either form.
        cleaned = value.strip().rstrip("/")
        if cleaned.endswith("/v1"):
            cleaned = cleaned[: -len("/v1")]
        return cleaned

    @model_validator(mode="after")
    def _guard_inprocess_backend(self) -> "Settings":
        """Refuses the no-isolation backend outright in production.

        ``inprocess`` runs model-generated code via a guarded ``exec()`` inside
        this same API process -- no separate process, no OS sandbox, nothing
        between a hallucinated ``os.environ`` write and every active session.
        It exists for CI and for environments where spawning a child is
        blocked, never as something a real deployment should be able to reach
        by a stray ``.env`` value.
        """
        if self.EXECUTION_BACKEND == "inprocess":
            if self.ENV == "prod":
                raise ValueError(
                    "EXECUTION_BACKEND=inprocess is refused when ENV=prod: it runs generated code with no "
                    "process or OS isolation in the API server itself. Use host (default) or docker."
                )
            logger.critical(
                "EXECUTION_BACKEND=inprocess selected -- generated code runs with NO isolation inside "
                "this process. Development/CI only; never set this in a real deployment.",
                env=self.ENV,
            )
        return self

    @model_validator(mode="after")
    def _size_to_the_host(self) -> "Settings":
        """Fills resource limits from the measured machine.

        Only fields the user did *not* set are touched -- ``model_fields_set``
        carries everything that came from the environment or the .env, so an
        explicit value always wins and this never silently overrides a choice.

        Without this the shipped defaults describe a server: eight inference
        threads (contention on a four-core laptop) and thirty-two sessions at
        2 GB each, which is sixty-four gigabytes of containers on a machine that
        may have four.
        """
        # Snapshotted: assigning below mutates `model_fields_set` in place, and
        # a later check would then read a field this method had just filled in
        # as though the user had chosen it.
        #
        # A blank string counts as unset. `docker-compose.yml` passes optional
        # knobs as `${SANDBOX_MEM_LIMIT:-}`, and an empty environment variable
        # is still *present* -- so without this, every compose deployment would
        # arrive here looking as though the operator had chosen "".
        explicit = {
            name
            for name in self.model_fields_set
            if not (isinstance(getattr(self, name, None), str) and not getattr(self, name).strip())
        }
        host = host_info()

        if "LLM_NUM_THREAD" not in explicit or self.LLM_NUM_THREAD <= 0:
            # Physical cores. More threads than cores makes local inference
            # slower, not faster -- the work is memory-bandwidth bound.
            self.LLM_NUM_THREAD = max(2, min(16, host.cores))

        if "QUEUE_MAX_WORKERS" not in explicit:
            self.QUEUE_MAX_WORKERS = max(1, min(4, host.cores // 2))

        ram = host.ram_bytes

        if "LLM_NUM_CTX" not in explicit or self.LLM_NUM_CTX <= 0:
            # Sized to what the prompts actually reach, not to what the model
            # permits. Everything built here is budgeted -- the dataset context
            # by column relevance, the transcript by `observation_chars` -- so a
            # full-tier prompt lands near 6k tokens and a compact one near 2k.
            # Asking for more than that does not admit a longer prompt; it
            # reserves KV cache that then has to be found for every resident
            # model, which is how a laptop ends up evicting the worker to make
            # room for the manager on every iteration.
            self.LLM_NUM_CTX = {"laptop": 8192, "server": 16384, "hpc": 32768}.get(host.profile, 8192)

        if not host.containerised:
            # `host.docker.internal` is how a container reaches its host and is
            # right *in* compose. Outside one it is a name Docker Desktop happens
            # to add to the hosts file, so it fails on a machine without Docker —
            # precisely the install the local backend exists to serve.
            for name in LOCAL_PROVIDERS:
                field = (describe(name).url_field if describe(name) else "") or ""
                if not field or field in explicit:
                    continue
                current = str(getattr(self, field, "") or "")
                setattr(self, field, current.replace("host.docker.internal", "127.0.0.1"))

        if "SANDBOX_MEM_LIMIT" not in explicit and ram:
            # An eighth of RAM per sandbox: enough for a real frame, small
            # enough that several sessions plus a local model still fit.
            per_sandbox = max(512 * 1024**2, min(4 * 1024**3, ram // 8))
            self.SANDBOX_MEM_LIMIT = format_memory(per_sandbox)

        if "SESSION_MAX_ACTIVE" not in explicit and ram:
            # Cap concurrent sandboxes so they cannot collectively claim more
            # than half of RAM, whatever the per-sandbox limit works out to.
            budget = ram // 2
            per_sandbox = parse_memory(self.SANDBOX_MEM_LIMIT) or (1024**3)
            self.SESSION_MAX_ACTIVE = max(1, min(32, int(budget // per_sandbox)))

        if "HOST_RUNTIME_MEM_LIMIT" not in explicit:
            # The host runtime is bounded like a container, so switching
            # backends does not silently change how much memory code may take.
            self.HOST_RUNTIME_MEM_LIMIT = self.SANDBOX_MEM_LIMIT

        # `openai` used to mean "whatever GATEWAY_API_URL says". It now means
        # api.openai.com, so an install that had pointed it at its own gateway
        # keeps working only if that configuration is carried across.
        if self.API_PROVIDER == "openai" and not self.OPENAI_BASE_URL.strip() and self.GATEWAY_API_URL.strip():
            self.OPENAI_BASE_URL = self.GATEWAY_API_URL.strip()
            if not self.OPENAI_API_KEY.strip():
                self.OPENAI_API_KEY = self.GATEWAY_API_KEY

        return self

    @property
    def system_profile(self) -> str:
        """The profile in force, with ``auto`` already resolved to the host."""
        if self.SYSTEM_PROFILE != "auto":
            return self.SYSTEM_PROFILE
        return host_info().profile

    # ------------------------------------------------------------------ #
    # Provider resolution
    #
    # A session may run each role on a different backend, so nothing may read
    # a provider's URL directly -- it has to go through these, keyed by the
    # provider actually in play for that call.
    # ------------------------------------------------------------------ #
    def resolve_provider(self, provider: str | None = None) -> str:
        """Falls back to the configured default for an empty or unknown value."""
        candidate = (provider or "").strip().lower()
        return candidate if candidate in PROVIDERS else self.API_PROVIDER

    def provider_root_url(self, provider: str | None = None) -> str:
        """Root URL of this backend, before any API-surface suffix."""
        descriptor = describe(self.resolve_provider(provider))
        if descriptor is None:  # pragma: no cover - resolve_provider guarantees one
            return ""
        configured = str(getattr(self, descriptor.url_field, "") or "").strip() if descriptor.url_field else ""
        return (configured or descriptor.default_base_url).rstrip("/")

    def provider_openai_base_url(self, provider: str | None = None) -> str:
        """The base an OpenAI-compatible client should be pointed at."""
        descriptor = describe(self.resolve_provider(provider))
        root = self.provider_root_url(provider)
        return f"{root}{descriptor.openai_suffix}" if descriptor and root else root

    def provider_api_key(self, provider: str | None = None) -> str:
        """This provider's key: the environment first, then the local store.

        Environment wins so a container or a CI run keeps behaving as configured;
        the store is what the settings UI writes to.
        """
        name = self.resolve_provider(provider)
        descriptor = describe(name)
        if descriptor is None:  # pragma: no cover - resolve_provider guarantees one
            return ""
        configured = str(getattr(self, descriptor.key_field, "") or "").strip() if descriptor.key_field else ""
        if configured:
            return configured

        # Imported here: this module loads before most of `core`.
        from src.core.credentials import credential_store

        return credential_store.get(name)

    def provider_is_configured(self, provider: str | None = None) -> bool:
        """Whether this provider has somewhere to connect to, and a key if it needs one."""
        name = self.resolve_provider(provider)
        descriptor = describe(name)
        if descriptor is None:  # pragma: no cover - resolve_provider guarantees one
            return False
        if not self.provider_root_url(name):
            return False
        return bool(self.provider_api_key(name)) if descriptor.requires_key else True

    @property
    def data_mode(self) -> str:
        """The mode in force, with the empty "derive it" value resolved.

        Derived rather than defaulted so upgrading a cloud-configured install does
        not break it in the name of protecting it.
        """
        chosen = self.DATA_MODE.strip().lower()
        if chosen in ("local-only", "cloud-only", "hybrid"):
            return chosen
        return "cloud-only" if is_cloud(self.API_PROVIDER) else "local-only"

    # ------------------------------------------------------------------ #
    # Agent budgeting
    # ------------------------------------------------------------------ #
    def resolve_tier(self, parameter_size: str | None = None) -> AgentTier:
        """The tier to run this turn at.

        An explicit ``AGENT_TIER`` always wins. On ``auto`` the tier is inferred
        from the manager model's reported parameter count, which is the only
        signal available without benchmarking: Ollama reports it per tag, LM
        Studio reports it per model, and hosted gateways report nothing -- for
        which ``balanced`` is the right assumption.
        """
        if self.AGENT_TIER != "auto":
            return self.AGENT_TIER  # type: ignore[return-value]
        return tier_for_parameter_size(parameter_size)

    def budget_for(self, mode: str = "auto", parameter_size: str | None = None) -> TierBudget:
        """Concrete limits for one turn, given the mode and the model behind it."""
        tier = self.resolve_tier(parameter_size)
        budget = TIER_BUDGETS[tier]

        if mode == "fast":
            # One shot: write code, run it, answer. No investigation, and no
            # verification either -- a second execution plus an extra worker
            # round-trip is the single most expensive thing the turn could do,
            # and the user asking for `fast` has said they do not want it.
            return replace(
                budget,
                iterations=1,
                allow_reflection=False,
                allow_verification=False,
                observation_chars=min(budget.observation_chars, self.AGENT_OBSERVATION_CHARS),
            )

        iterations = budget.deep_iterations if mode == "deep" else budget.iterations
        return replace(
            budget,
            iterations=min(iterations, self.AGENT_MAX_ITERATIONS),
            observation_chars=min(budget.observation_chars, self.AGENT_OBSERVATION_CHARS),
            # `deep` restores the decision round-trip even on a compact model.
            # The tier's answer to "should this model steer itself" is a default
            # about what is worth paying for, and someone who reached for `deep`
            # has said the cost is acceptable. Leaving it off would make the
            # control a no-op on exactly the setup where the user is most likely
            # to reach for it -- a small model that gave a shallow first answer.
            allow_decisions=budget.allow_decisions or mode == "deep",
        )

    def output_budget(self, purpose: str) -> int:
        """Tokens one kind of call may produce, clamped to ``MAX_TOKENS``.

        Clamped rather than maxed: ``MAX_TOKENS`` is the ceiling someone lowers
        when their context is small, and a per-purpose budget must not quietly
        raise it back.
        """
        budgets = {
            "plan": self.LLM_MAX_TOKENS_PLAN,
            "decision": self.LLM_MAX_TOKENS_DECISION,
            "code": self.LLM_MAX_TOKENS_CODE,
            "answer": self.LLM_MAX_TOKENS_ANSWER,
            "review": self.LLM_MAX_TOKENS_REVIEW,
        }
        return max(64, min(self.MAX_TOKENS, budgets.get(purpose, self.MAX_TOKENS)))

    @property
    def cors_origins(self) -> list[str]:
        """Parsed CORS allowlist. `*` is honoured but disables credentialed requests."""
        raw = [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",")]
        return [origin for origin in raw if origin]

    @property
    def cors_allow_credentials(self) -> bool:
        # Sending credentials with a wildcard origin is rejected by every browser
        # and is a spec violation, so the two settings are resolved here once.
        return "*" not in self.cors_origins

    @property
    def redis_enabled(self) -> bool:
        return bool(self.REDIS_URL.strip())

    # ------------------------------------------------------------------ #
    # Execution backend
    #
    # Which backends this configuration permits. Whether one is *reachable* is
    # a runtime question and belongs to `core.tools.runtime`, which is why these
    # only express permission -- config cannot import the sandbox without a
    # circular import, and should not need to.
    # ------------------------------------------------------------------ #
    @property
    def docker_backend_allowed(self) -> bool:
        # Named explicitly or not at all: a container is no longer something an
        # install falls into by having Docker running.
        return self.SANDBOX_ENABLED and self.EXECUTION_BACKEND == "docker"

    @property
    def host_backend_allowed(self) -> bool:
        # Also true under `docker`, which degrades to a host subprocess when no
        # daemon is reachable. Only `inprocess` forbids spawning outright.
        return self.EXECUTION_BACKEND != "inprocess"

    @property
    def sandbox_image(self) -> str:
        """Image tag for the current tier, unless one was named explicitly."""
        return self.SANDBOX_IMAGE.strip() or f"wizard-sandbox:{self.SANDBOX_TIER}"

    @property
    def host_runtime_mem_bytes(self) -> int:
        """Memory ceiling for a host runtime, 0 when uncapped."""
        return parse_memory(self.HOST_RUNTIME_MEM_LIMIT) or 0


settings = Settings()

# Ensure directories exist
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
settings.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
