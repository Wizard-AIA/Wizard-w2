# Backend Architecture & System Overview

> High-level architecture map and index for the Wizard w2 backend.
> Concise agent instructions live in [`backend/CLAUDE.md`](../backend/CLAUDE.md).

---

## System Overview

Wizard w2 is a local-first autonomous data analysis agent. A FastAPI backend
orchestrates a "manager" reasoning model and a "worker" code-generation model,
running generated Python inside an isolated execution sandbox and streaming
reasoning, plan, code, stdout, and the answer over a single WebSocket.

```
Client (Next.js) ───[ WebSocket / REST ]───► FastAPI Transport
                                                   │
                                        AnalysisOrchestrator.run
                                                   │
                                          ┌────────┴────────┐
                                          ▼                 ▼
                                    Manager (LLM)     Worker (LLM)
                                          │                 │
                                          └────────┬────────┘
                                                   ▼
                                           CodeExecutor.guard
                                                   │
                                                   ▼
                                         Runtime Daemon (TCP)
                                      (Host Subprocess / Docker)
```

---

## Subsystem Architecture Index

The deep architectural reference is divided into topic-focused documents:

| Topic | Document | Key Subsystems & Areas |
|---|---|---|
| **Agent Loop & Workflow** | [`docs/agent-loop.md`](agent-loop.md) | Single request path, event protocol frames, orchestrator loop, plan & permission gates, deterministic compact decisions, subagent concurrency & proxy sessions, trust layer grounding & verification, export |
| **Security & Permissions** | [`docs/security.md`](security.md) | Data mode enforcement (`local-only`/`cloud-only`/`hybrid`), per-prompt schema redaction, permission profiles & categories, `ConsentBroker` suspension, credentials & OS permissions |
| **Runtime & Infrastructure** | [`docs/runtime.md`](runtime.md) | Execution backends (`host`/`docker`/`inprocess`), daemon protocol & preloading, session state, reference documents, context budgeting & capability filtering, configuration & host sizing, SQLite persistence, testing architecture |
| **Sandbox & Isolation** | [`docs/sandbox.md`](sandbox.md) | Container limits, OS-native sandbox (Linux Landlock/seccomp, macOS SBPL, Windows job objects/integrity levels), two-phase enforcement, selftest probe |
| **Connectors & Ingest** | [`docs/connectors.md`](connectors.md) | Snapshot ingest model, driver registry, SQL reflection, credential references, consent gates, and multi-lock write-back safety |
| **Skills System** | [`docs/skills.md`](skills.md) | Layered registry (built-in/user/project), YAML subset parser, query-coverage ranking, promotion pipeline, GitHub install pipeline, trust boundary |
| **LLM, Providers & Memory** | [`docs/llm.md`](llm.md) | Provider descriptor table, model registry caching, memory footprint estimation & keep-alive swapping, reasoning model streaming, usage tracking, model downloader, embeddings |

---

## Core Architectural Invariants

1. **One Request Path**: `POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`. Transports contain no business logic.
2. **Loop, Not Pipeline**: Each iteration the manager inspects real stdout and decides the next action dynamically.
3. **Defense in Depth**: Code is validated by `CodeGuard` AST rules first, then executed inside an OS/Docker contained runtime.
4. **Local-First Safety**: `local-only` hard-refuses cloud models and outbound search tools at provider resolution.
5. **Deterministic Fallbacks**: Weak or compact models (<4B) use deterministic execution decisions rather than wasting turns deliberating.
