# Backend Architecture

> Deep reference for the Wizard w2 backend. The concise rules live in
> [`backend/CLAUDE.md`](../backend/CLAUDE.md); this file explains **why**
> those rules exist and records design rationale, historical context, and
> implementation details that are useful when doing deep work on a subsystem.

---

## One Request Path

`POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`. The
transport only translates events into frames — it contains no workflow logic.

Historically the WebSocket handler re-implemented the node loop by hand, and
the two copies drifted until the semantic cache and the fast-path router
applied to REST only.

---

## Event Protocol — `events.py`

The orchestrator knows nothing about WebSockets; it emits typed events to an
`Emitter`. `EventCollector` buffers them for the REST path and for tests;
`WebSocketEmitter` serialises them onto a socket.

### Frame catalogue

| Frame | Notes |
|---|---|
| `session` | |
| `status` | |
| `step_start` / `step_end` | |
| `reasoning_delta` | split from `plan_delta` during streaming by tracking the `<thought>` tag boundary incrementally (`_stream_plan`) |
| `plan_delta` | |
| `content_delta` | |
| `code` | |
| `stdout` | |
| `artifact` | |
| `approval_required` | carries an `id` **only** for a mid-run permission gate — its presence tells the client the turn is paused rather than ended |
| `warning` | |
| `error` | |
| `final` | |
| `iteration_start` | agentic loop |
| `action` | |
| `observation` | closes the most recent `action` that has none — never correlated by id |
| `finding` | |
| `plan_revised` | |
| `assumption` | |
| `verification` | |
| `skill` | names which skill informed the turn; emitted rather than left implicit, because a frame is the only way it is true on screen |
| `skill_candidate` | offer to save an analysis; `kind` distinguishes `recurring` vs `recovery` |
| `usage` | emitted **only when a cloud model ran** — under `local-only` silence is the honest surface |
| `SUBAGENT_START` / `SUBAGENT_END` | bound a branch's lifetime for the UI; everything inside emits existing frame types tagged with `branch` by `BranchEmitter` |

---

## Workflow — `orchestrator.py`

**This is a loop, not a pipeline.** Each iteration the manager sees what has
actually run and chooses the next move; the run ends when it says it can
answer, or the budget is spent.

```
orient (plan) → [plan gate] → loop → verify → answer
                                ↑ ↓
                 inspect / code+execute / consult / reflect
                          ↓         ↓
              [permission gate]  correct   (bounded by MAX_CORRECTION_RETRIES)
```

### Gates

- **Plan gate**: ends its turn, resumed by starting a new one. Opt-in
  (`AGENT_REQUIRE_APPROVAL`). A plan containing `SEARCH: "…"` halts for
  consent regardless — that leaves the machine. Under `local-only` the search
  is **refused** rather than gated.
- **Permission gate**: **suspends** and resumes in place. See consent section.
- An approved plan skips `_orient` entirely (where the gate lives), so it
  cannot re-fire. It must also not be downgraded to `fast`.

### Actions

Actions live in `actions.py`. `parse_decision` **never raises** — malformed
model output resolves to a default (`code` mid-run, forced `answer` on the
last iteration). `inspect` is answered deterministically from the frame
(`Session.inspect`), costing no LLM call.

### Budgets and modes

- Budgets come from `settings.budget_for(mode, parameter_size)`.
- Modes: `auto` (agent picks depth), `fast` (one shot, **no verification**),
  `deep`. `planning` is a legacy alias meaning "deep, gate the plan".
- **Below the balanced tier the loop decides deterministically**
  (`TierBudget.allow_decisions=False`). `_decide_deterministically` reads the
  answer off what happened: a step that succeeded and printed something means
  stop, anything else means write code. Turns a nine-call compact turn into a
  three-call one. `deep` restores the round-trip on every tier.
- The same deterministic function is the *default* on every tier when the
  model's answer is unparseable.
- `AGENT_TURN_TIMEOUT` is checked **before an iteration is claimed** — never
  mid-call, not after `iterations_used` is incremented. Verification is the
  first thing it gives up.

### Motivation (DABstep)

The old shape fixed a plan before touching the data and fed 200 characters of
each step's output into the next. It could not recover when the data
contradicted the plan. [DABstep](https://arxiv.org/abs/2506.23719) measures
the gap: hard tasks need 6+ dependent steps, planning is the largest error
category.

---

## Subagents — `orchestrator.py`

`parallel` is a fourth kind of step alongside `code`/`consult`/`reflect`: the
manager fans one step out into several concurrent, isolated mini-investigations.

Offered only when `settings.SUBAGENT_ENABLED and budget.allow_subagents and
budget.max_subagents >= 2` — the compact tier never sees it.

### Key implementation details

- **Real concurrency needs one runtime per branch.** The daemon protocol is
  single-in-flight per process (one `accept()` loop, one shared `exec_globals`,
  a process-global stdout swap). `Session.spawn_subagent_id` mints a composite
  id (`f"{session.id}{CHILD_DELIMITER}{branch}"`) that gets its own runtime,
  workspace and usage-ledger bucket.

- **`SubagentSession` is a structural proxy, not a subclass.** It overrides
  `.id`, `.executor`, `.workspace`; everything else forwards to the parent
  through `__getattr__`. Concurrent branches share one `PermissionState` —
  two branches can both prompt for the same subject before either grants.

- **`_split_subgoals` splits on `\s+\|\s+`, not a bare `|`.** A goal like
  "count rows where status matches A|B" would otherwise read as two sub-goals.
  Fewer than two real sub-goals degrades to a plain `_act_code` step.

- **Child id is qualified by `group`** (`f"{group}-sub{index+1}"`), not just
  the branch label — a second `parallel` decision reuses `"sub1"` and without
  the group prefix that would collide.

- **Each branch runs a bounded, deterministic mini-loop** (`_run_subagent`,
  reusing `_act_code`) — no decision or verification round-trip inside it.
  The parent verifies once at the end.

- **`inprocess` runs branches serially, not concurrently.** That backend has
  no per-call isolation. Real backends (`host`, `docker`) run branches through
  `asyncio.gather` under a shared deadline.

- **Cost rolls up automatically.** Every LLM call books under its composite
  id, so `usage_ledger.totals_many([session.id, *state.subagent_ids])` is what
  makes the readout include subagent spend. `release_subagent_runtime` frees
  a branch's process immediately but does **not** forget its usage-ledger entry;
  full teardown (`dispose_subagent`) waits for the whole turn to end.

---

## Trust Layer — `grounding.py`

Deterministic, no LLM calls, and it **reports rather than edits** —
post-processing model output is exactly the mistake above.

- `check_grounding` flags numbers in the answer that appear in no execution
  output. Tolerance comes from the *answer's own* precision (`3.14` for an
  output of `3.14159` is reporting, not invention) plus magnitude words
  (`1.23 million`).
- `assumptions_from_code` reads silent decisions back out of the code that
  ran — `dropna`, `how='inner'`, `nlargest`, `errors='coerce'`. Each changes
  what the number means.
- `_verify` re-derives the headline result by a different route and looks for
  `VERIFIED:` / `MISMATCH:`. A wrong join grain produces a confident,
  plausible, wrong number that no self-review catches.

---

## Execution — `execution.py` + `tools/runtime.py`

`CodeExecutor.execute` is the **only** way generated code reaches an
interpreter. It guards first, then hands the code to whichever runtime
`runtime.active_backend()` names.

Semantic cleaning on upload goes through here too — but only when there is
something to clean: `flow._needs_cleaning` looks for the three problems
`create_cleaning_prompt` actually names and skips the model entirely when it
finds none.

### Backend table

| Value | What runs the code | Isolation |
|-------|--------------------|----|
| `host` (default) | one subprocess per session | separate process with `RLIMIT_AS`, timeout, interrupt — not yet a security boundary |
| `docker` | one container per session | process, filesystem, network, memory, PIDs, caps |
| `inprocess` | guarded `exec` in the API process | none, and the namespace does not persist |

### Legacy spellings

`auto` and `local` are the pre-w2 spellings and are folded to `host` by a
`mode="before"` field validator, so an existing `.env` keeps working.

### Docker opt-in

Docker is opt-in: reached only when named, and naming it on a machine with no
reachable daemon degrades to `host` with a warning. That reverses an earlier
rule which resolved an unreachable Docker to `inprocess` — `inprocess` is the
least contained runtime, so that answered "your container is missing" by
removing the isolation that remained.

`ExecutionResult.isolation` (`container` / `os-sandbox` / `process` / `none`)
is what the UI keys on; `sandboxed` is derived from it.

`local` is not a degraded mode and does not warn per message; only `inprocess`
does. Docker remains the right answer for input you did not write yourself.

Both real backends run the **same daemon** over the same protocol.

### Workspace paths

**Any path handed to generated code must come from
`runtime.workspace_path(session_id, name)`.** A literal `/workspace` is only
real inside a container; on the local backend it names a directory that does
not exist. `plot_output_path` and `prompts._workspace_root` are the same
helper wearing different names; keep them delegating rather than re-deriving.

---

## Daemon — `tools/daemon.py`

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
  because a Windows path inside a string literal is a set of escape sequences.
  `repr` is correct on every platform and keeps native separators.
- `capabilities` probes with `find_spec`, so it costs a path search rather
  than twenty imports.
- `DaemonClient` owns the protocol; `SandboxSession` and `HostSession` add
  only lifecycle.

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

## Export — `agent/export.py` + `routes/export.py`

Turns a turn's *real executed steps* — pulled from the investigation, never
reconstructed from the model's description — into a runnable script or
notebook. The same grounding rule applies: report what ran, don't launder it.

- **Two callers, one builder.** The always-on per-turn artifact
  (`orchestrator._write_script`) and the on-demand `GET /api/export/{message_id}`
  route both go through `export.build_script`/`build_notebook`.
- **Two loader shapes.** The always-on artifact reads Feather files in place;
  the on-demand export ships CSV copies (`bundle_files`) and reads those.
  `dataset_loader_lines` takes the file template and pandas reader as
  parameters.
- **A connector-sourced table is never embedded.** It is looked up by name at
  run time through `ConnectionStore.by_name`.
- **Chat messages persist each turn's real steps** (`chat_messages.meta`)
  specifically so a turn stays exportable after a later turn overwrites the
  workspace's single `analysis.py`.
- **A zip, not a bare file, whenever a file-based table needs to travel with
  the script.** The route decides per export.
- **User-initiated only**, from the results UI.

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
**word boundaries**. A substring test looks equivalent and is not: a column
called `C` matches inside "check" and `id` matches inside "provide".

`prompts.TOOLKIT` is a **catalogue, not a promise**. `_toolkit_block(session_id)`
filters it through `runtime.capabilities(session_id)`, which asks the runtime
what it can actually import. Entries are **atomic** — one naming three
libraries is dropped unless all three are present.

`_visualization_rules` and `_workspace_root` are capability- and
backend-aware: `PLOT_FORMAT=html` needs plotly (which `core` tier does not
ship), and the writable root differs per backend.

`runtime.TIER_MODULES` mirrors the Dockerfile for the case where no runtime
exists yet. A test asserts the two agree.

---

## Data Mode — `core/data_mode.py`

**This is the mechanism behind the local-first promise.** Before it, "your
data stays local" was a property of how somebody happened to configure their
`.env`.

`local-only` / `cloud-only` / `hybrid`, session-wide, seeded from
`settings.data_mode`.

**Enforcement lives in `LLMProvider.resolve`** — the one function all nine LLM
call sites already pass through. A violation raises `DataModeViolation`.

Three axes:
- **mode** — which providers a role may resolve to.
- **policy** (`DataPolicy`) — how much of the data a cloud-bound prompt carries.
- **tools** — `web_search` is *unavailable* under `local-only`. `SEARCH:`
  directive is dropped with a warning. `disabled_tools()` feeds the UI.

Switching mode **clears any role assignment the new mode forbids**.

Embeddings are a role like any other — but a forbidden encoder **degrades to
the hashing fallback** instead of raising.

---

## Redaction — `should_redact` + `prompts.py`

`generate_system_context(..., redact=True)` keeps shape, column names, dtypes,
null rates and semantic types, and drops every real value. It adds a line
telling the model values were withheld.

**Decided per prompt, from the provider that prompt is going to**, not once per
turn. Under `hybrid` with a cloud manager and a local worker, the planner is
redacted and the code generator is not.

**Execution output is deliberately *not* redacted.** The answer is synthesised
from real stdout by `create_answer_prompt`; withholding it would leave the
answering model nothing to answer from.

Settable **per source** as well as per session (`DataPolicy.per_dataset`).
"Follow default" is a real third state. An override is dropped with its
dataset.

---

## Permission Profile — `core/permissions.py` + `agent/consent.py`

**Two independent dials.** Depth (`fast`/`auto`/`deep`) is how hard the agent
works; the profile (`auto-approve`/`ask-always`/`custom`) is how often it
stops to ask. **Data mode outranks the profile, always.**

### Categories

| category | live | trigger |
|---|---|---|
| `library_install` | yes | `imported_modules(code) - runtime.missing_modules(...)`, checked **before** execution |
| `network` | yes | the plan's `SEARCH:` directive; installing a skill from GitHub |
| `workspace_write` | yes | a literal path the guard rejected, defaulting to `deny` |
| `db_connect` | yes | opening a saved connection |
| `db_write` | yes | writing a session table back to a source, `always_ask`, subject `connection:table` |
| `tool_use` | **no** | declared for a later milestone |

### Key rules

- `db_write` carries `always_ask=True`: it never resolves to `allow` from a
  profile, and `set_ruling` raises rather than silently clamping.
- **Default is `ask-always`, not `auto-approve`.** Defaulting to auto-approve
  would have made an upgrade silently stop asking about web search.
- **The guard is not weakened.** A `workspace_write` grant records the
  directory in `PermissionState.extra_roots`; the code is then re-scanned.
  `GuardVerdict.only_paths` ensures a program mixing a path violation with a
  banned import is never offered for consent.
- Grants are session-scoped and **not persisted**. Tightening clears them.
- `AGENT_REQUIRE_APPROVAL` stays separate — that gate is about the plan,
  turn-terminating. It is wrong for an action chosen at iteration four, which
  is why `consent.py` exists.

### ConsentBroker

`ConsentBroker.ask` parks the turn on a future keyed by session id. Timeout,
cancel, disconnect and `abandon()` all resolve as denied. `orchestrator.run(can_prompt=...)`
is how a transport declares it has a reply channel; REST and CLI pass `False`.

A denial does **not** end the turn. `_act_code` records a failed `Step` and
returns, so the loop routes around it.

`chat.py`'s receive loop no longer awaits the run — an `approval` frame with
an `id` is routed to the broker before the "a run is already in progress"
check.

---

## Credentials — `core/credentials.py`

Keys live in `credentials.json` under the platform config directory
(`utils/appdirs.py`). `WIZARD_CONFIG_DIR` overrides it; the test suite pins it.

This is not encryption at rest — the guarantee is the OS's access control.
OS keychain integration is deliberately not taken (three platform backends plus
a dependency, and Secret Service is often absent on headless Linux).

### File permissions

- POSIX: `0600`.
- Windows: inheritance stripped, single-user ACL via `icacls` granted to the
  **SID from the process token** (`whoami /user`), never `%USERNAME%`.
  Result verified afterwards; rolled back to inherited permissions if the file
  came back unwritable.

Resolution order: **environment/settings first, then the store.** Keys are
never logged and **never returned by any route**.

---

## Config — `config.py`

Pydantic-settings singleton reading `backend/.env`.

### Key rules

- `API_PROVIDER` is the *default* provider, not a global switch. `MODEL_TYPE`
  exists only for old `.env` compatibility.
- **`DATA_MODE` empty means "derive it"**: `local-only` on a fresh install,
  `cloud-only` when `API_PROVIDER` is already cloud. `DATA_SCHEMA_ONLY`
  defaults **on**.
- `openai` now means `api.openai.com`. A gateway URL carries across only when
  `API_PROVIDER` is `openai`.
- `LMSTUDIO_BASE_URL` strips `/v1` suffix. LM Studio binds loopback only until
  "Serve on Local Network" is enabled.
- `LLM_NUM_CTX` reaches Ollama only. Derived from the host (laptop 8192 /
  server 16384 / hpc 32768); `0` means "derive it". It is a *load-time*
  parameter.
- **`SYSTEM_PROFILE`**: on `auto` the host is measured at boot
  (`utils/hostinfo.py`) and `LLM_NUM_THREAD`, `QUEUE_MAX_WORKERS`,
  `SANDBOX_MEM_LIMIT`, `HOST_RUNTIME_MEM_LIMIT`, `SESSION_MAX_ACTIVE` are
  derived. Derivation runs in `model_validator(mode="after")` and only fills
  fields the user did not set. A blank string counts as unset.
- `hostinfo` is under `utils/`, not `core/infra/`: `Settings` is constructed
  at import time and `core.infra.__init__` imports the cache, which imports
  `settings` back.
- Host sizing and `AGENT_TIER` are **separate axes**.
- `HOST_SANDBOX` (`off`/`best-effort`/`require`), `HOST_SANDBOX_NETWORK`
  (`deny`/`allow`), `EXECUTION_BACKEND` (`host`/`docker`/`inprocess`),
  `SANDBOX_TIER` (`core`/`standard`/`full`).
- **`MODEL_NAME` / `WORKER_MODEL_NAME` / `VISION_MODEL_NAME` are empty by
  default**, meaning "use whatever is installed".
- `AGENT_TIER` (`auto`/`compact`/`balanced`/`full`): on `auto` inferred from
  parameter count. Gateways get `balanced`.
- `settings.budget_for(mode, parameter_size)` is the single place mode and
  tier combine. `fast` → `allow_verification=False`. `deep` →
  `allow_decisions=True` on every tier.
- `TierBudget.max_columns` reaches the worker and planner prompts, not only
  `inspect`.
- `AGENT_MAX_ITERATIONS` is a hard ceiling, deliberately not derived.
- `SKILLS_BUILTIN_DIR` / `SKILLS_PROJECT_DIR` are empty by default. The user
  root is always `config_dir()/skills`.
- `PLOT_FORMAT` is coupled across `create_prompt` and `_execute`. Change both.
- `cors_origins` / `cors_allow_credentials` are resolved together: wildcard
  origin forces credentials off.
- Every local provider's base URL is rewritten from `host.docker.internal` to
  `127.0.0.1` when not containerised. The shipped `.env.example` leaves both
  commented out.

---

## Infrastructure — `infra/`

`get_cache()` and `get_queue()` return in-process implementations by default.
`REDIS_URL` (with `redis` installed) swaps in Redis; if Redis is unreachable,
the cache degrades. Nothing requires Redis.

---

## Persistence — `database.py`

One SQLite file, `backend/data/wizard.db`, through `db_mgr`. **Connections are
pooled per thread and closed explicitly**, with WAL and a busy timeout.
FastAPI dispatches blocking work through `asyncio.to_thread`.

Tables: `semantic_cache`, `trajectories`, `feedbacks`, `working_memory`,
`chat_messages`, `schema_registry`. Additive column migrations run on boot
from the `MIGRATIONS` tuple.

---

## Image Size

Measured, not estimated.

| Image | Was | Now | How |
|-------|-----|-----|-----|
| backend API | torch + 11 CUDA wheels = **2.8 GB** | none of it | embeddings via the provider |
| sandbox | `xgboost` = **154 MB** | `xgboost-cpu`, **4.5 MB** | plus `SANDBOX_TIER` layers |
| frontend | `node_modules`, **580 MB** | `.next/standalone`, **30 MB** | `output: "standalone"` |

The backend Dockerfile installs with `--compile`. `PYTHONDONTWRITEBYTECODE=1`
is set for the runtime.

---

## Testing Architecture

Four layers under `backend/tests/`: `unit/`, `integration/`, `regression/`,
`negative/`.

### Stubs

Shared LLM stubs live in `tests/stubs.py` and the `stub_llm` fixture in
`conftest.py`. `backend/tests` is on `sys.path` via `conftest.py`, so
`from stubs import ScriptedLLM` works from any test module — do not
cross-import between test files. Running out of scripted responses yields
`"Done."` rather than raising.

### Call count

The loop changes the call *count*, not just the content: a `fast` run is
plan → code → answer, an `auto` run adds a decision call per iteration plus a
verification call. A test that scripts N responses and gets `"Done."` is
usually missing the verification entry.

### Regression tests

`regression/test_regressions.py` pins specific defects; each test's docstring
states what broke and why. Read it before changing session handling, the
database layer, the guard, the rate limiter, provider resolution or
`sandbox.interrupt()`.

`regression/test_turn_cost.py` pins what one turn is allowed to cost —
round-trip count per tier, per-purpose output budgets, that no chain of thought
is re-read on a later call, and that a turn out of time still answers.
`RecordingLLM` records `max_tokens` per call. A streamed call must record
**once**: `ScriptedLLM.stream_to` forwards its kwargs to `astream`.

### conftest.py pinning

- `EXECUTION_BACKEND=inprocess`, `SANDBOX_ENABLED=false`, `HOST_SANDBOX=off`,
  `EMBEDDINGS_FORCE_FALLBACK=true` — set before importing `src` (Settings
  instantiates at import time).
- `WIZARD_CONFIG_DIR` → temp directory (no real credentials).
- `DATA_MODE=hybrid`, `DATA_SCHEMA_ONLY=false`.
- `SKILLS_BUILTIN_DIR` / `SKILLS_PROJECT_DIR` → empty temp directories.
- `SKILLS_REGISTRY_API` → `http://127.0.0.1:1`.
- `OLLAMA_BASE_URL`, `LMSTUDIO_BASE_URL`, `OPENAI_BASE_URL`,
  `ANTHROPIC_BASE_URL` → `http://127.0.0.1:1`.
- Autouse teardown clears `semantic_cache`, `usage_ledger`,
  `db_mgr.clear_skill_candidates()`, `skill_registry.clear_user_skills()`,
  `install_index`, staging root.
- Autouse fixture stubs `tools.packages.install`.
- `backend/tests/sandbox/` spawns a process; skipped unless
  `WIZARD_SANDBOX_SELFTEST=1` is set.
