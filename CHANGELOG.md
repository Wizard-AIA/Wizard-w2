# Changelog

All notable changes to Wizard are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions before this
file existed are reconstructed from tags, release notes, and milestone commits.

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

[Unreleased]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.0.0-w2-planning...HEAD
[v2.0.0-w2-planning]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.1...v2.0.0-w2-planning
[v2.2.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.1...v2.2.0
[v2.1.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.0...v2.1.1
[v2.1.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/Wizard-AIA/Wizard-w2/releases/tag/v2.0.0
