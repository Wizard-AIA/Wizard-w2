# Changelog

All notable changes to Wizard are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions before this
file existed are reconstructed from tags, release notes, and milestone commits.

## [v1.0.8] - 2026-09-06

### Fixed
- **Clean Execution Runtime & Monkeypatch Removal:** Eliminated ad-hoc runtime overrides on `pd.DataFrame` and `scikit-learn` in `execution.py`, restoring standard, predictable Python library semantics with AST-based security controls.
- **Universal Data Engineering Standards in Prompting:** Enforced core data science best practices across code generation prompts (numeric isolation for aggregations/correlations, NaN inspection and imputation, linear model coefficient array flattening, and strict schema adherence).
- **Context-Enriched Self-Correction:** Augmented execution error prompts to include the executed `<failed_code>`, live `<runtime_diagnostics>` (actual DataFrame shape, columns, precise dtypes, and null counts), enabling models to self-heal dynamically across any dataset.
- **Remote GPU Acceleration Tuning:** Optimized Ollama context budgeting (`LLM_NUM_CTX=4096`) ensuring 100% GPU VRAM residence (0% CPU offload) on 8 GB cards (e.g. RTX 2060 SUPER) with ~85 t/s on `qwen3:4b` and ~55 t/s on `qwen2.5-coder:7b` over Tailscale.
- **KaTeX Math Rendering:** Enhanced LaTeX equation delimiters (`$$...$$`, `\[...\]`, `\(...\)`), normalized escaped delimiters, and fixed paragraph DOM nesting in frontend markdown rendering.

## [v1.0.7] - 2026-08-30

### Fixed
- **Markdown Table Block Parsing:** Fixed a parsing bug where tables preceded by paragraphs were swallowed into plain text. Table header detection (`| Column | ...`) and row boundaries now parse accurately and render into interactive, styled data tables with zebra striping and tabular numbers.
- **Gemini Free Tier Model Prioritization:** Updated default model suggestions to prioritize high-capacity Flash models (`gemini-2.5-flash` and `gemini-3.7-flash`) over quota-exhausted preview models (`gemini-3.1-pro`), ensuring zero-friction out-of-the-box performance for Google AI Studio keys.
- **LLM Error Sanitization:** Added intelligent exception sanitization that parses HTTP 429 quota/rate-limit and 401 authentication errors into clean, actionable advice with retry timers instead of raw JSON exception tracebacks.
- **WebSocket Session Auto-Minting:** WebSocket connection handshakes with stale or expired session tokens now self-heal by automatically minting fresh sessions, preventing infinite reconnect loops on browser refresh.
- **Sandbox Capability Diagnostics:** Refined macOS capability diagnostics to cleanly report OS-level security constraints (Apple Silicon seatbelt deprecation and `RLIMIT_AS` memory capping behavior) with clear recommendations for Docker kernel sandboxing.
- **Default Execution Runtime:** Defaulted execution backend to `host` with auto-approve permissions for seamless autonomous data analysis out of the box.

## [v1.0.6] - 2026-08-30

### Added
- **Tri-Model System Architecture:** Formalized the architecture into three decoupled, first-class model roles: **Manager** (hypothesis generation, reasoning, synthesis), **Worker** (Python/SQL code generation, execution self-correction), and **Embeddings** (semantic chunk indexing, hybrid search, RAG retrieval). Each role can be independently configured across **Local** (air-gapped Ollama/LM Studio), **Hybrid** (selective schema redaction), and **Cloud** (frontier model APIs), with zero-disk deterministic Blake2b hashing fallback.
- **User Choice for Local & Cloud Embeddings:**
  - Added `--embedding-provider` and `--embedding-model` flags to `wizard init`.
  - Updated `wizard init --pull-models` to automatically pull `nomic-embed-text` on Ollama by default, ensuring semantic vector search works out-of-the-box.
  - Added `EMBEDDING_PROVIDER` and `EMBEDDING_MODEL` status diagnostics to `wizard status` / `wizard doctor`.
  - Added dynamic embedding configuration in `GET /api/config` and `PATCH /api/config` with hot re-warming.
  - Added an **Embeddings & Vector Model** card in the Frontend Settings Workbench with active backend status indicators.
- **SRE Health Honesty & Kubernetes Probes:**
  - Added dedicated `/health/live` (process liveness) and `/health/ready` (cluster readiness probing SQLite writeability, Redis reachability, and Docker socket health with HTTP 503 on degradation).
  - Implemented single-flight cache stampede protection and atomic session execution locks to eliminate race conditions.
  - Implemented online WAL checkpointing and automated background SQLite backup scheduler with retention pruning (`backups/`).
  - Added Dead Letter Queue (DLQ) routing with exponential backoff for failed async analysis jobs.
- **Dynamic Hybrid Vector & BM25 Search Engine:**
  - Integrated `sqlite-vec` KNN cosine vector search with FTS5 BM25 keyword search using Reciprocal Rank Fusion (RRF).
  - Integrated `CrossEncoderReranker` with FlashRank ONNX cross-attention scoring.
  - Batched chunk vectorization (`encode_many`) to eliminate single-item HTTP round-trip overhead.
- **Enterprise Distributed Observability & Metrics:**
  - Full OpenTelemetry distributed tracing (`telemetry.py`) with W3C `traceparent` propagation across HTTP, WebSockets, background tasks, and supervisor daemons. Zero-overhead `DummyTracer` fallback when unconfigured.
  - Added Prometheus metrics (`/metrics`) exposing rolling p50/p90/p95/p99 latency quantiles, Time-To-First-Token (TTFT), and token generation counters.
  - Added `SessionEventBus` powered by Redis Pub/Sub with in-process `asyncio.Queue` fallback for horizontal multi-worker state synchronization.
- **Stateful Resumable DAG Agent Architecture:**
  - Implemented `ExecutionDAG`, `DAGNode`, and `DAGEdge` in `dag.py` with Kahn's algorithm cycle detection, topological dependency ordering, and JSON checkpoint serialization (`to_dict` / `from_dict`) for graceful crash recovery.
- **Multi-Worker Scaling, Reverse Proxy & Server-Sent Events (SSE):**
  - Added production `deploy/nginx.conf` with least-connection upstream balancing, IP rate limiting (`limit_req_zone`), WebSocket upgrades, and `proxy_buffering off`.
  - Added `POST /api/chat/stream` SSE fallback for environments blocking bidirectional WebSockets or experiencing buffer shedding.
  - Added `RedisJobQueue` with distributed Redis list leasing (`brpop`) and worker heartbeats.
- **Sandbox Security Hardening & Continuous Verification:**
  - Implemented `validate_container_config` enforcing read-only root filesystems (`read_only=True`), unprivileged non-root users (`user="1000:1000"`), and blocking `/var/run/docker.sock` mounts.
  - Added automated canary rollout script (`scripts/canary_deploy.py`) and GitHub Actions workflow (`.github/workflows/canary.yml`) with automated rollback if error rate > 1.0% or p95 latency > 3.0s.
  - Added CI benchmark drift detector (`scripts/check_benchmark_drift.py`) gating quality drops > 2%, and mutation testing harness (`scripts/run_mutation_tests.py`).
  - Added cell-by-cell Markdown table numerical grounding check (`check_table_grounding`).
  - Added in-process `SLMRouter` fast-tracking metadata and chitchat turns.

### Fixed
- Fixed silent 200 OK responses on degraded dependencies in `/health`.
- Fixed WebSocket buffer overflows during heavy token streams by implementing a 256-frame bounded backpressure queue with client frame shedding.
- Fixed unbounded memory growth in SQLite analysis logs via automatic date-partitioned pruning (`prune_old_analysis_data`).

## [v1.0.5] - 2026-08-28

### Fixed
- **Dataset Preview "No rows to show" Bug:** Corrected PyArrow schema inference in `_arrow_chunks` — schema was derived from a 0-row empty slice (`df.iloc[:0]`), causing `pa.null()` type inference for string/object columns and an `ArrowInvalid` exception that silently aborted the streaming response. Schema is now inferred from actual data rows.
- **CORS Missing Arrow Headers:** Added `X-Arrow-Total-Rows` and `X-Arrow-Offset` to the CORS `expose_headers` list so frontend pagination can read total row count from the response.
- **DataGrid Loading Flash:** Initialized `loading` state to `true` in `data-grid.tsx` to prevent a momentary "No rows to show" flash before the fetch fires.

### Changed
- **Universal Model Filtering:** Added `EXCLUDED_MODEL_PATTERNS` allowlist in `registry.py` that filters out embeddings, TTS, audio, image/video generation, robotics, deep-research, and other non-chat endpoints from cloud providers (Gemini, OpenAI, Anthropic). Gemini model list reduced from 54 raw models to 18 usable chat/coding/reasoning models. `models/` prefix is stripped, and capabilities (`vision`, `code`, `reasoning`, `chat`) are tagged per model.
- **Local Provider Preservation:** Ollama and LM Studio model lists no longer run through the aggressive cloud exclusion filter — local models are user-installed intentionally, so only name normalization is applied. Fixes 3 failing unit tests in `test_providers.py`.

### Added
- **Settings UI Environment Controls:** Added comprehensive controls in the Settings Workbench for `API_PROVIDER`, `DATA_MODE`, `DATA_SCHEMA_ONLY`, `GATEWAY_API_URL`, `GATEWAY_API_KEY`, `SUBAGENT_MAX_ITERATIONS`, `SANDBOX_EXEC_TIMEOUT`, `COUNCIL_ENABLED`, `VISION_ENABLED`, `CONTEXT_DOCS_ENABLED`, and `SKILLS_ENABLED` — all with full persistence to `backend/.env` via the `PATCH /api/config` endpoint.

## [v1.0.4] - 2026-08-28

### Added
- **Global Persistent Workspace State Across Route Navigation:**
  - Implemented top-level `WorkspaceProvider` encapsulating streaming turn state, active WebSocket connection, dataset management, and investigation trail at the root layout. Navigating across `/`, `/data`, `/skills`, `/models`, and `/settings` preserves active turn execution and chat history without interruptions or session loss.
- **Real-Time Datasets Sidebar & Live Execution Pulse:**
  - Integrated a persistent dataset panel in the main navigation sidebar with active dataset indicators, row/column counts, quick switching, and a global dataset loader trigger.
  - Added real-time "Live" pulsing status badge indicating background turn execution during navigation.
- **Interactive Runtime & Environment Settings Workbench:**
  - Replaced read-only setting views with a full-featured control panel for execution backends (`host`, `docker`, `inprocess`), host OS sandboxing, outbound network policies, agent reasoning depths, verification gates, grounding checks, temperature, context windows, and cloud/local LLM provider configurations.
  - Added `PATCH /api/config` and `POST /api/config` backend endpoints with atomic `.env` file persistence.
- **Synchronized OpenAPI & TypeScript Contracts:**
  - Generated and validated end-to-end type safety across backend schemas and frontend TypeScript models.

## [v1.0.3] - 2026-08-28

### Added
- **Universal 1-Command Installers for Linux, Windows & macOS:**
  - `curl -fsSL https://wizardw2.vercel.app/install.sh | bash` for automated Linux installation (Ubuntu, Debian, Fedora, Arch, Alpine, RHEL) with dynamic kernel arch detection (`x86_64`, `aarch64`), HTTP/1.1 resilient downloads, and shell PATH injection.
  - `irm https://wizardw2.vercel.app/install.ps1 | iex` for Windows 10/11 PowerShell with persistent User Environment registry updates.
  - Scoop package manager manifest (`scoop install https://wizardw2.vercel.app/wizard.json`).
  - Homebrew tap integration (`brew tap Wizard-AIA/wizard && brew install wizard`).
- **Fixed 3-Column Documentation Architecture:**
  - Redesigned documentation shell with pinned, independently scrollable sidebars (`Left Navigation` | `Middle Reading Content` | `Right Table of Contents`), preventing sidebars from detaching or scrolling offscreen during document navigation.
- **Dedicated Blueprint Architecture Diagram Canvas:**
  - High-contrast blueprint canvas with non-ligature monospace font grid and `System Architecture & Flow` validation badges for all ASCII and workflow diagrams.
- **Global CLI Symlink Path Resolution:**
  - Resolved `os.Executable()` symlink traversal in `cli/internal/repo/repo.go`, enabling `wizard` supervisor commands to execute from any arbitrary system directory outside the repository checkout.
- **CodeGuard AST & Sandbox Hardening:**
  - Expanded static security policy across 31 banned modules, 11 builtins, and 22 dunder reflection patterns.
  - Graceful `LLMUnavailableError` handling for missing local/cloud provider SDKs.

## [v1.0.2] - 2026-08-26

### Added
- **Ecosystem Documentation Overhaul:** Refreshed MkDocs documentation site with custom styling (`extra.css`), glassmorphism card layouts, and interactive Mermaid architecture flowcharts.
- **Community Skill Registry:** Populated the `skills` repository with curated domain skills (`cohort-analysis`, `data-quality-triage`, `outlier-detection`, `time-series-forecasting`) and added automated `registry.json` compilation.
- **Standardized Issue Forms:** Cross-platform GitHub issue forms with dropdown selectors for Operating System, Execution Backend, and LLM Provider.
- **Organization Profile Refresh:** Direct standalone download matrix for macOS (arm64/amd64), Linux (amd64/arm64), and Windows.

## [v1.0.1] - 2026-08-25

### Added
- **Polars Engine Integration:** Added Polars alongside DuckDB and Pandas for fast, multi-threaded DataFrame processing on large datasets (#113).
- **Smart Tiered Task Router:** Deterministic classifier for `LIGHTWEIGHT`, `STANDARD`, and `REASONING_HEAVY` turns with safe dynamic downscaling to smaller installed models (#117).
- **Zero-Copy Apache Arrow Streaming:** Binary Arrow IPC streaming endpoint (`/api/workspace/stream-arrow`) and frontend decoder for instant large dataset previews (#114).
- **Continuous Fuzzing & Security:** Property-based fuzz testing for AST code guards and file ingest headers, plus OpenSSF Scorecard token permissions (#104).
- **Caching & RAG:** Integrated Semantic Result Cache (#115) and Dynamic Skill RAG retrieval (#116).

## [v1.0.0] - 2026-08-25

### Added
- **First Consumer Release:** Shipped pre-compiled standalone zip packages for macOS (arm64/amd64), Linux (amd64/arm64), and Windows (amd64).
- **Supervised CLI Daemon:** Introduced `./cli/wizard init` and `./cli/wizard start` supervisor binary.
- **Evidence-Backed Control Plane:** Multi-hypothesis tracking, adversarial verification, result grounding checks, and transparent assumption extraction.
- **OS-Native Sandboxing:** Secure subprocess execution with Landlock/seccomp on Linux, `sandbox-exec` on macOS, and Windows Job Objects.
- **Autonomous Feedback Loop:** Manager/Worker ReAct agent cycle with automatic Python traceback recovery.

## [v2.0.0-w2-planning] - 2026-08-07

Wizard w2 — a from-the-ground-up evolution of the w1 codebase, delivered as
ten milestones (see `docs/wizard-evolution-spec.md` for the full spec):

1. Provider-agnostic model layer with an explicit local/cloud/hybrid data mode
2. Two independent dials — agent depth and permission profile
3. Host-primary execution with Docker made optional, OS-native sandboxing
   (Landlock/seccomp on Linux, `sandbox-exec` on macOS, a job object on Windows)
4. Expanded data connectivity — relational databases and object storage
5. A `SKILL.md`-based skills system the agent can cite and promote to
6. A GitHub-based public skill registry, with install/update/diff review
7. Subagents for parallel, isolated sub-investigations
8. `wizard`, a single static Go binary managing the stack as a background
   service, replacing the manual `uvicorn`/`npm run dev`/`docker compose` dance
9. Re-runnable export (script/notebook) and unified results actions
10. Versioning, docs, and release polish for the w2 line

### Added
- `MAINTAINERS.md`, upstream URL fixes for the org migration.

## [v2.2.1] - 2026-01-30
Dependency bumps and CI housekeeping.

## [v2.2.0] - 2026-01-29
### Added
- Documentation link-checking in CI (Lychee).
### Changed
- CI Action version bumps.

## [v2.1.1] - 2026-01-29
### Added
- Repository CI/CD, code-quality, and automation workflows; README badges.
### Changed
- README clarity pass; model-availability and Docker-prerequisite disclaimers.

## [v2.1.0] - 2026-01-29
### Added
- `MODEL_PATH` environment variable and a read-only backend volume mount.

## [v2.0.0] - 2026-01-29
Initial public foundation: FastAPI backend (CSV upload, chat, validation),
the first agent framework and skills, and the CI/CD bootstrap (linting,
dependency auditing, API contract tests).

[Unreleased]: https://github.com/Wizard-AIA/Wizard-w2/compare/v1.0.6...HEAD
[v1.0.6]: https://github.com/Wizard-AIA/Wizard-w2/compare/v1.0.5...v1.0.6
[v1.0.5]: https://github.com/Wizard-AIA/Wizard-w2/compare/v1.0.4...v1.0.5
[v2.0.0-w2-planning]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.1...v2.0.0-w2-planning
[v2.2.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.1...v2.2.0
[v2.1.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.0...v2.1.1
[v2.1.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/Wizard-AIA/Wizard-w2/releases/tag/v2.0.0
