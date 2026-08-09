# Wizard w2 Technical Documentation Hub

Welcome to the technical documentation for **Wizard w2**, a local-first autonomous data analysis agent.

---

## Documentation Index

### Core Architecture & Agent Loop
- [`docs/architecture.md`](architecture.md) — System topology, cross-cutting architectural invariants, and subsystem directory index.
- [`docs/agent-loop.md`](agent-loop.md) — Orchestrator loop, event protocol frames, plan & permission gates, deterministic decisions, subagents, grounding/trust layer, and script/notebook export.
- [`docs/runtime.md`](runtime.md) — Execution backends (`host`/`docker`/`inprocess`), daemon TCP protocol & preloading, session state, reference documents, context budgeting, and SQLite persistence.

### Security, Sandboxing & Ingest
- [`docs/security.md`](security.md) — Data mode enforcement (`local-only`/`cloud-only`/`hybrid`), per-prompt schema redaction, permission profiles, `ConsentBroker` suspension, and OS credential security.
- [`docs/sandbox.md`](sandbox.md) — Docker container containment, OS-native sandbox implementation (Linux Landlock/seccomp, macOS SBPL, Windows job objects/integrity levels), and selftest probes.
- [`docs/connectors.md`](connectors.md) — Snapshot data connector ingest model, SQL reflection, credential references, consent gates, and multi-lock write-back safety.

### Skills & LLM Engine
- [`docs/skills.md`](skills.md) — Layered skill registry (built-in/user/project), YAML parser constraints, query-coverage ranking, candidate promotion, and GitHub install pipeline.
- [`docs/llm.md`](llm.md) — Provider descriptor system, model registry caching, memory footprint estimation & keep-alive swapping, reasoning model streaming, usage ledgers, and model downloading.

### Frontend Application
- [`docs/frontend.md`](frontend.md) — Next.js 16 / React 19 route architecture, WebSocket lifecycle under StrictMode, trust layer rendering, live state store subscriptions, and design system tokens.

---

## Agent Instruction Files

- [`CLAUDE.md`](../CLAUDE.md) — Global project guidelines & stack commands.
- [`backend/CLAUDE.md`](../backend/CLAUDE.md) — Backend-specific execution, testing, and security invariants.
- [`frontend/CLAUDE.md`](../frontend/CLAUDE.md) — Frontend-specific routing, React state, and design system rules.
- [`cli/CLAUDE.md`](../cli/CLAUDE.md) — CLI daemon background supervision and cross-compilation rules.
