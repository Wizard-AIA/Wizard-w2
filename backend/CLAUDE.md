# Backend

Backend-specific architecture, commands and test policy.
Loads only when work touches `backend/`. Global rules: [root CLAUDE.md](../CLAUDE.md).

## Commands

```bash
# Install
uv pip install --system -r requirements.txt              # API server only (root file)
uv pip install --system -r requirements-local.txt         # analysis toolkit (Docker-less)
uv pip install --system -r requirements-optional.txt      # Redis / OpenAI gateway

# Run
uvicorn src.api.api:app --reload --port 8000              # from backend/
python backend/main.py path/to/data.csv                   # CLI REPL

# Test (run from repo ROOT — pyproject sets testpaths/pythonpath)
pytest
pytest backend/tests/unit -q
pytest backend/tests/unit/test_code_guard.py::test_repair_strips_markdown_fences

# Lint
ruff check . --fix && ruff format .                       # CI: ruff check + ruff format --check
```

## Testing

`asyncio_mode = "auto"` — async tests need no decorator.

### Environment invariants

**Tests never touch Docker, Ollama or the network, and never spawn a process.** `conftest.py` sets these env vars *before* importing `src` (Settings instantiates at import time):

- `EXECUTION_BACKEND=inprocess`, `SANDBOX_ENABLED=false`, `HOST_SANDBOX=off`, `EMBEDDINGS_FORCE_FALLBACK=true`
- `WIZARD_CONFIG_DIR` → temp dir (no real credentials)
- `DATA_MODE=hybrid`, `DATA_SCHEMA_ONLY=false`
- `SKILLS_BUILTIN_DIR`, `SKILLS_PROJECT_DIR` → empty temp dirs
- `SKILLS_REGISTRY_API`, `OLLAMA_BASE_URL`, `LMSTUDIO_BASE_URL`, `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL` → `http://127.0.0.1:1`

Keep new env pinning at the top of `conftest.py`.

`EXECUTION_BACKEND=inprocess` is load-bearing: `SANDBOX_ENABLED=false` alone means only "no Docker", and `host` would spawn a child importing pandas for every session.

### Autouse fixtures and teardown

- Stubs `tools.packages.install` (prevents real pip; consent tests still work because the stub preserves the gate question, just blocks the actual install)
- Clears: `semantic_cache` (not just `db_mgr.clear_cache()` — the in-process exact-match cache must also be cleared), `usage_ledger`, `db_mgr.clear_skill_candidates()`, `skill_registry.clear_user_skills()`, `install_index`, staging root

### Stubs

Shared LLM stubs in `tests/stubs.py` + `stub_llm` fixture. `from stubs import ScriptedLLM` works from any test module — **do not cross-import between test files.** Out-of-responses yields `"Done."` not an exception.

### Call count matters

The loop changes call count: `fast` = plan → code → answer; `auto` adds decision + verification calls. A test scripting N responses that gets `"Done."` is usually missing the verification entry.

### Regression tests

- `test_regressions.py` — pins specific defects with docstrings. Read before changing session handling, database, guard, rate limiter, provider resolution, or `sandbox.interrupt()`.
- `test_turn_cost.py` — pins round-trip count per tier, per-purpose output budgets, no chain-of-thought re-read, out-of-time still answers. `RecordingLLM` records `max_tokens`. Streamed calls record **once** (via `astream`).

### Sandbox self-tests

`backend/tests/sandbox/` spawns a process; skipped unless `WIZARD_SANDBOX_SELFTEST=1`. CI never sets it.

## Architecture Map

```
src/
├── api/           REST + WebSocket transport (no workflow logic)
├── config.py      Pydantic-settings singleton (reads backend/.env)
├── providers.py   Provider descriptor table (beside config.py, not under core/llm/)
├── core/
│   ├── agent/     Orchestrator, actions, events, grounding, consent, export
│   ├── connectors/  Database/object-store ingest (snapshot, not live pushdown)
│   ├── credentials.py
│   ├── data_mode.py   local-only / cloud-only / hybrid enforcement
│   ├── database.py    SQLite persistence (WAL, pooled per thread)
│   ├── embeddings.py  Provider → sentence-transformers → hashing fallback
│   ├── execution.py   The ONLY path generated code reaches an interpreter
│   ├── ingest/        Upload + document ingestion
│   ├── llm/           Provider clients, registry, resources, reasoning, usage
│   ├── permissions.py  Permission profiles + categories
│   ├── prompts.py     System context, toolkit, redaction
│   ├── rag/           Retriever, context budgeting
│   ├── security/      CodeGuard (AST), OS sandbox
│   ├── session.py     Per-browser session state
│   ├── skills/        Layered registry, promotion, GitHub install
│   └── tools/         Daemon, host/Docker runtime, packages, search
└── utils/         appdirs, fileperms, hostinfo, logging
```

## Invariants

### One request path

`POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`. The transport translates events into frames — it **must not** contain workflow logic.

### Event protocol

- Orchestrator emits typed events to an `Emitter`; knows nothing about WebSockets.
- `approval_required` carries an `id` **only** for a mid-run permission gate — its presence distinguishes a paused turn from an ended one.
- `observation` closes the most recent `action` that has none — never correlated by id.
- `usage` emitted **only when a cloud model ran**; under `local-only`, silence is the honest surface.
- See [docs/agent-loop.md](../docs/agent-loop.md) for frame catalogue and streaming protocol.

### Agent loop

**Loop, not pipeline.** Each iteration the manager sees what actually ran and chooses the next move.

```
orient (plan) → [plan gate] → loop → verify → answer
                                ↑ ↓
                 inspect / code+execute / consult / reflect
                          ↓         ↓
              [permission gate]  correct   (bounded by MAX_CORRECTION_RETRIES)
```

- Two gates:
  - **Plan gate**: ends the turn; approval starts a new turn. Opt-in (`AGENT_REQUIRE_APPROVAL`).
  - **Permission gate**: suspends the current turn and resumes in place.
- An approved plan skips `_orient` entirely (cannot re-fire) and must not be downgraded to `fast`.
- `parse_decision` **never raises** — malformed output → default (`code` mid-run, `answer` on last iteration).
- `inspect` is deterministic from the frame (no LLM call).
- Modes: `auto` (agent picks depth), `fast` (one shot, **no verification**), `deep`. `planning` = legacy alias for "deep + gate the plan".
- **Below balanced tier**: deterministic decisions (`allow_decisions=False`). Succeeded-and-printed → stop; otherwise → code. `deep` restores the round-trip on every tier.
- `AGENT_TURN_TIMEOUT` checked **before** an iteration is claimed, never mid-call.
- Under `local-only`, `SEARCH:` is **refused** (not gated) — no consent can make it allowed.
- **The final answer is synthesised by the manager from real execution output** (`create_answer_prompt`). Do not reintroduce client-side cleanup.
- See [docs/agent-loop.md](../docs/agent-loop.md) for full loop and grounding details.

### Subagents

- `parallel` step: offered only when `SUBAGENT_ENABLED and budget.allow_subagents and max_subagents >= 2`. Compact tier never sees it.
- `SubagentSession` is a structural proxy (not a subclass); overrides `.id`, `.executor`, `.workspace`; forwards everything else.
- `inprocess` runs branches **serially**; real backends use `asyncio.gather`.
- See [docs/agent-loop.md](../docs/agent-loop.md) for full subagent design.

### Execution

- `CodeExecutor.execute` is the **only** way generated code reaches an interpreter.
- Three backends: `host` (default, subprocess), `docker` (container), `inprocess` (guarded exec, test/dev only).
- Docker is **opt-in**; unreachable daemon degrades to `host` with a warning.
- **Any path to generated code must come from `runtime.workspace_path(session_id, name)`** — a literal `/workspace` only exists in containers.
- Both real backends run the **same daemon** over the same protocol. See [docs/runtime.md](../docs/runtime.md).

### CodeGuard

- AST-based (not regex). Blocks banned modules, builtins, dunder attrs, `__builtins__`, computed/dunder reflection, literal paths outside writable roots.
- `CodeGuard.scan(code, extra_roots=...)` — local runtime passes the session directory.
- Drive-letter paths: `_is_path_allowed` folds backslashes and checks for `X:/` explicitly.
- Policy violation → stop. Syntax error → retryable (`verdict.syntax_error`).
- `GuardVerdict.only_paths` — a program mixing path violation with banned import is never offered for consent.

### Daemon

- `DAEMON_SCRIPT` is a string literal (`put_archive` into a stock image). Render via `render_daemon()`. It is `%`-formatted — **no bare `%` allowed**.
- TCP protocol: length-prefixed (`>I`) JSON. Actions: `execute`, `inspect_variables`, `reload_dataset`, `reset`, `capabilities`, `ping`.
- `df` preloaded from `<workspace>/dataset.feather`; all tables from `<workspace>/tables/*.feather`. `remove_dataset` must call `reload_dataset()`.
- Paths interpolated with **`%r`**, not into `"..."` (Windows escape sequences).
- `capabilities` probes with `find_spec` (no imports).
- See [docs/runtime.md](../docs/runtime.md) for daemon protocol details.

### Sessions

- Every browser gets a `Session` (own datasets, documents, catalog, history, workspace, container). No global dataset state.
- `DatasetHandle.table_key`: sanitised name for generated code (`Q3 sales (final).csv` → `tables['q3_sales_final']`).

### Data mode

- `local-only` / `cloud-only` / `hybrid`, session-wide.
- **Enforcement in `LLMProvider.resolve`** — the one function all LLM call sites pass through.
- `local-only` **refuses** cloud providers (hard boundary, not preference).
- Three axes: mode (which providers), policy (how much data), tools (web_search unavailable under local-only).
- Switching mode clears forbidden role assignments.
- Forbidden encoder degrades to hashing fallback (doesn't raise).
- See [docs/security.md](../docs/security.md) for full data mode policy.

### Permissions

- Profile (`auto-approve`/`ask-always`/`custom`) is independent of depth (`fast`/`auto`/`deep`). **Data mode outranks profile.**
- Categories: `library_install`, `network`, `workspace_write`, `db_connect`, `db_write` (live); `tool_use` (reserved).
- Default is `ask-always`. `db_write` has `always_ask=True` — `set_ruling` raises on attempt to override.
- Grants are session-scoped, not persisted. Tightening clears them.
- `ConsentBroker.ask` parks the turn on a future. Timeout/cancel/disconnect → denied. `orchestrator.run(can_prompt=...)` declares reply channel availability.
- A denial does **not** end the turn — the loop routes around it.
- See [docs/security.md](../docs/security.md) for permissions and consent broker details.

### Redaction

- `generate_system_context(..., redact=True)` keeps schema, drops values.
- Decided **per prompt**, from the provider that prompt goes to. Not once per turn.
- **Execution output is not redacted** (the answer is synthesised from it).
- Per-source overrides via `DataPolicy.per_dataset`.

### Credentials

- Platform config dir (`utils/appdirs.py`). Resolution: env/settings first, then store.
- **Never returned by any route** — only `has_key` and masked hints.
- Permissions: POSIX `0600`, Windows SID-based ACL (via `whoami /user`, not `%USERNAME%`).
- See [docs/security.md](../docs/security.md) for credential storage design.

### Context budgeting

- `generate_system_context` selects columns by relevance; `mentions_column` matches on **word boundaries** (not substrings).
- `prompts.TOOLKIT` is a catalogue, not a promise — filtered by `runtime.capabilities()`. Entries are **atomic** (all-or-nothing).
- `_visualization_rules` and `_workspace_root` are capability- and backend-aware.
- `runtime.TIER_MODULES` mirrors the Dockerfile; a test asserts agreement.
- See [docs/runtime.md](../docs/runtime.md) for context and prompt budgeting.

### LLM / providers

- Provider is **per-request**, not process-wide. `ModelSpec` carries resolved `base_url` (part of cache key). **Never read provider URL from `settings` directly** — use `settings.provider_root_url` / `provider_openai_base_url` / `provider_api_key`.
- `providers.py` is beside `config.py`, not under `core/llm/` (import cycle avoidance).
- `is_cloud()` treats unknown providers as cloud (safe direction for data-mode check).
- `settings.output_budget("decision"|"plan"|"code"|"answer"|"review")` — pass the budget for the call's purpose. `max_tokens` is part of the client cache key.
- See [docs/llm.md](../docs/llm.md) for memory fitting, registry, embeddings, downloading.

### Reasoning models

- `split_reasoning` / `strip_reasoning` remove `<think>`, `<thinking>`, `<thought>`, `<reasoning>`, `<reflection>` blocks. `ReasoningStream` does this incrementally.
- **`_extract_code` strips reasoning first** — a model drafts code inside `<think>`, discards it, writes the real thing. Searching raw response runs the rejected draft.
- An unclosed block yields empty visible text.

### Skills

- Three layers (ascending precedence): built-in → user-global → project. Built-in is **read-only**.
- **A skill may never carry executable code** — enforced in `loader.load_skill` (refuses, naming the file).
- `frontmatter.py` is a **restricted YAML subset** (not PyYAML).
- Retrieval: planning prompt + `consult` action. **Not the worker prompt** (would be paid N times). No LLM call.
- **Install is user-initiated only — never an agent action** (anti-worm rule).
- See [docs/skills.md](../docs/skills.md) for full design (promotion, GitHub install, scoring).

### Connectors

- Snapshot, not live pushdown. Generated code never holds a connector or opens a socket.
- Registry keyed by `kind`, not URL scheme. Drivers probed with `find_spec`, never imported.
- `ConnectionSpec` carries a reference to a credential, never the credential itself.
- `spec.dataset_name()` is the only builder for table keys. `CONNECTOR_MAX_ROWS` is not optional.
- Consent: saving a connection is not gated; **opening** one is. Write-back has three locks.
- See [docs/connectors.md](../docs/connectors.md) for full design.

### Config

- Pydantic-settings singleton reading `backend/.env`.
- `API_PROVIDER` = default provider, not global switch. `MODEL_NAME`/`WORKER_MODEL_NAME`/`VISION_MODEL_NAME` empty by default.
- `DATA_MODE` empty = derive (`local-only` fresh, `cloud-only` if cloud provider set). `DATA_SCHEMA_ONLY` defaults on.
- `AGENT_TIER` (`auto`/`compact`/`balanced`/`full`); `auto` inferred from parameter count. Host sizing is a **separate axis**.
- `settings.budget_for(mode, parameter_size)` is the single place mode + tier combine.
- `AGENT_MAX_ITERATIONS` is a hard ceiling, not derived.
- `LLM_NUM_CTX` reaches Ollama only; derived from host; `0` = derive.
- `SYSTEM_PROFILE` on `auto`: host measured at boot, derivation fills only unset fields.
- `PLOT_FORMAT` coupled across `create_prompt` and `_execute` — **change both**.
- See [docs/runtime.md](../docs/runtime.md) for full config details.

## Conventions

Ruff line-length 120, `E501` disabled (formatter owns line length).

## Deep Documentation

For design rationale, historical context, and implementation details beyond these rules:

- [docs/architecture.md](../docs/architecture.md) — System overview, subsystem map, and architecture index
- [docs/agent-loop.md](../docs/agent-loop.md) — Orchestrator, loop, subagents, events, grounding, export
- [docs/security.md](../docs/security.md) — Data mode, permission profiles, consent broker, redaction, credentials
- [docs/runtime.md](../docs/runtime.md) — Execution backends, daemon protocol, session state, config derivation, database, testing
- [docs/sandbox.md](../docs/sandbox.md) — Docker backend, OS sandbox (Linux/macOS/Windows), two-phase enforcement, selftest
- [docs/connectors.md](../docs/connectors.md) — Connector architecture, registry, credential handling, consent, write-back
- [docs/skills.md](../docs/skills.md) — Skill layers, promotion pipeline, GitHub install flow, trust boundary
- [docs/llm.md](../docs/llm.md) — Provider system, model registry, memory fitting, reasoning, usage, downloading, embeddings
