# Runtime, Execution & Infrastructure

> Deep reference for Wizard w2 execution backends, the daemon protocol, sessions,
> configuration derivation, persistence, context budgeting, and testing architecture.
> Concise rules live in [`backend/CLAUDE.md`](../backend/CLAUDE.md).

---

## Execution Backends — `execution.py` + `tools/runtime.py`

`CodeExecutor.execute` is the **only** way generated code reaches an
interpreter. It guards first, then hands the code to whichever runtime
`runtime.active_backend()` names.

Semantic cleaning on upload goes through here too — but only when there is
something to clean: `flow._needs_cleaning` looks for the three problems
`create_cleaning_prompt` actually names and skips the model entirely when it
finds none.

### Backend Table

| Value | What runs the code | Isolation |
|---|---|---|
| `host` (default) | One subprocess per session | Separate process with `RLIMIT_AS`, timeout, interrupt — not yet a full security boundary |
| `docker` | One container per session | Process, filesystem, network, memory, PIDs, caps |
| `inprocess` | Guarded `exec` in the API process | None, and the namespace does not persist |

### Legacy Spellings & Degradation

`auto` and `local` are pre-w2 spellings folded to `host` by a `mode="before"`
validator.

Docker is opt-in: reached only when named. Naming it on a machine with no
reachable daemon degrades to `host` with an announced warning (not silently to
`inprocess`).

`ExecutionResult.isolation` (`container` / `os-sandbox` / `process` / `none`)
is what the UI keys on; `sandboxed` is derived from it.

### Workspace Paths

**Any path handed to generated code must come from
`runtime.workspace_path(session_id, name)`.** A literal `/workspace` is only
real inside a container; on the local backend it names a directory that does
not exist. `plot_output_path` and `prompts._workspace_root` are the same
helper wearing different names.

---

## Daemon Protocol — `tools/daemon.py`

`DAEMON_SCRIPT` is a string literal because the container receives it over
`put_archive` into a stock Python image. Render it through `render_daemon()`;
it is `%`-formatted, so **no bare `%` may appear in it**.

- Length-prefixed (`>I`) JSON over TCP. Actions: `execute`,
  `inspect_variables`, `reload_dataset`, `reset`, `capabilities`, `ping`.
- `df` is **not** passed per call; the daemon preloads it from
  `<workspace>/dataset.feather`. `Session._materialize` writes it;
  `reload_dataset` refreshes it without restarting the runtime.
- **Every** session table is preloaded into `tables['<table_key>']` from
  `<workspace>/tables/*.feather`, with `df` still bound to the active one.
  `remove_dataset` must call `reload_dataset()`, or the deleted frame stays
  queryable.
- `WORKSPACE` is parameterised: `/workspace` in a container, the session
  directory locally. Paths are interpolated with **`%r`**, not `"..."`,
  because a Windows path inside a string literal contains escape sequences.
  `repr` is correct on every platform and preserves native separators.
- `capabilities` probes with `find_spec`, so it costs a path search rather
  than twenty imports.
- `DaemonClient` owns the protocol; `SandboxSession` and `HostSession` add
  lifecycle management.

---

## Sessions — `session.py`

Every browser gets a `Session`: its own datasets, reference documents,
catalog, chat history, workspace directory and container. Resolved from the
`X-Session-Id` header (or `?session=`), TTL-reaped, and capacity-bounded.
There is no global dataset state.

`DatasetHandle.table_key` is the sanitised name generated code addresses the
table by (`Q3 sales (final).csv` → `tables['q3_sales_final']`). It also names
the file under `workspace/tables/`.

---

## Reference Documents — `ingest/documents.py`

Data dictionaries, metric definitions, business rules — `.md/.txt/.rst/.html`
always, `.pdf/.docx` when `pypdf`/`python-docx` are installed. Chunked on
**paragraph** boundaries, not fixed width: a definition cut in half yields two
chunks that each retrieve well and neither of which states the rule.

Retrieval goes through `embedding_service`, so it degrades to lexical overlap
with no model loaded.

`.txt` is deliberately claimed by both loaders — a tab-delimited export and a
plain-text dictionary are both real. The endpoint decides which. Nothing
structured (`.csv`, `.parquet`, `.xlsx`) may appear in the document list.

---

## Context Budgeting — `retriever.py` + `prompts.py`

`generate_system_context` does not dump the whole frame. Columns are selected
by relevance to the question (columns named in the question are always kept),
and memories, trajectories and few-shot examples are retrieved semantically.
Everything degrades to lexical scoring when no embedding model is loaded.

"Named in the question" goes through `mentions_column`, which matches on
**word boundaries**. Substring matching would over-match (`C` in "check", `id`
in "provide").

`prompts.TOOLKIT` is a **catalogue, not a promise**. `_toolkit_block(session_id)`
filters it through `runtime.capabilities(session_id)`. Entries are **atomic**
(dropped unless all constituent libraries are present).

`_visualization_rules` and `_workspace_root` are capability- and
backend-aware: `PLOT_FORMAT=html` needs plotly (which `core` tier does not
ship), and writable roots differ per backend.

`runtime.TIER_MODULES` mirrors the Dockerfile for when no runtime exists yet.

---

## Configuration & Host Sizing — `config.py`

Pydantic-settings singleton reading `backend/.env`.

### Key Rules

- `API_PROVIDER` is the *default* provider, not a global switch.
- **`DATA_MODE` empty means "derive it"**: `local-only` on a fresh install,
  `cloud-only` when `API_PROVIDER` is cloud. `DATA_SCHEMA_ONLY` defaults **on**.
- `LMSTUDIO_BASE_URL` strips `/v1` suffix.
- `LLM_NUM_CTX` reaches Ollama only; derived from the host (laptop 8192 /
  server 16384 / hpc 32768); `0` means "derive it".
- **`SYSTEM_PROFILE`**: on `auto` the host is measured at boot
  (`utils/hostinfo.py`) and `LLM_NUM_THREAD`, `QUEUE_MAX_WORKERS`,
  `SANDBOX_MEM_LIMIT`, `HOST_RUNTIME_MEM_LIMIT`, `SESSION_MAX_ACTIVE` are
  derived. Derivation runs in `model_validator(mode="after")` and only fills
  fields the user did not set. A blank string counts as unset.
- `hostinfo` is under `utils/`, not `core/infra/` (avoids circular imports).
- Host sizing and `AGENT_TIER` are **separate axes**.
- **`MODEL_NAME` / `WORKER_MODEL_NAME` / `VISION_MODEL_NAME` are empty by
  default** (resolved via `model_registry.suggest`).
- `settings.budget_for(mode, parameter_size)` is the single place mode and
  tier combine.
- `PLOT_FORMAT` is coupled across `create_prompt` and `_execute`.
- Local provider URLs rewritten from `host.docker.internal` to `127.0.0.1`
  when running outside Docker.

---

## Infrastructure & Persistence

### Infrastructure — `infra/`

`get_cache()` and `get_queue()` return in-process implementations by default.
`REDIS_URL` (with `redis` installed) swaps in Redis; if Redis is unreachable,
the cache degrades gracefully.

### Persistence — `database.py`

One SQLite file, `backend/data/wizard.db`, through `db_mgr`. **Connections are
pooled per thread and closed explicitly**, with WAL and a busy timeout.
FastAPI dispatches blocking work through `asyncio.to_thread`.

Tables: `semantic_cache`, `trajectories`, `feedbacks`, `working_memory`,
`chat_messages`, `schema_registry`. Additive column migrations run on boot
from `MIGRATIONS`.

---

## Image Size

Measured, not estimated.

| Image | Was | Now | How |
|---|---|---|---|
| backend API | torch + 11 CUDA wheels = **2.8 GB** | none of it | embeddings via provider |
| sandbox | `xgboost` = **154 MB** | `xgboost-cpu`, **4.5 MB** | plus `SANDBOX_TIER` layers |
| frontend | `node_modules`, **580 MB** | `.next/standalone`, **30 MB** | `output: "standalone"` |

Backend Dockerfile installs with `--compile`. `PYTHONDONTWRITEBYTECODE=1` is
set for runtime.

---

## Testing Architecture

Four layers under `backend/tests/`: `unit/`, `integration/`, `regression/`,
`negative/`.

### Stubs

Shared LLM stubs live in `tests/stubs.py` and the `stub_llm` fixture in
`conftest.py`. `from stubs import ScriptedLLM` works across tests without
cross-test file imports. Out of responses yields `"Done."`.

### Regression Tests

- `test_regressions.py` pins specific historical defects.
- `test_turn_cost.py` pins token limits, round-trip counts, output budgets,
  and reasoning stripping.

### conftest.py Pinning

- `EXECUTION_BACKEND=inprocess`, `SANDBOX_ENABLED=false`, `HOST_SANDBOX=off`,
  `EMBEDDINGS_FORCE_FALLBACK=true` set before importing `src`.
- `WIZARD_CONFIG_DIR` → temp directory.
- `DATA_MODE=hybrid`, `DATA_SCHEMA_ONLY=false`.
- `SKILLS_BUILTIN_DIR` / `SKILLS_PROJECT_DIR` → empty temp directories.
- `SKILLS_REGISTRY_API`, `OLLAMA_BASE_URL`, `LMSTUDIO_BASE_URL`,
  `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL` → `http://127.0.0.1:1`.
- Autouse teardown clears caches, usage ledger, candidate tables, and staging.
- `tools.packages.install` is stubbed to prevent accidental network/pip calls.
- `backend/tests/sandbox/` skipped unless `WIZARD_SANDBOX_SELFTEST=1`.
