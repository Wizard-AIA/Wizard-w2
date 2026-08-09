# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Wizard w2** — a local-first autonomous data analysis agent. A FastAPI backend orchestrates a "manager" model that reasons and a "worker" model that writes code, each resolvable to a local provider (Ollama, LM Studio) or a cloud provider (Anthropic, OpenAI, a gateway) under an explicit, session-wide data mode. Generated Python runs in a per-session sandbox — a host subprocess under OS-native containment by default, or a Docker container if you opt into that — and streams reasoning, code, stdout and the final answer to a Next.js client over one WebSocket.

Monorepo: `backend/` (Python 3.11, FastAPI) + `frontend/` (Next.js 16 / React 19 / Tailwind v4) + `cli/` (Go, a single static binary managing both as a background service).

## Folder guides

This file covers the whole repo. Folder-specific architecture, commands and conventions live beside the code and load only when you work there: [backend/CLAUDE.md](backend/CLAUDE.md), [frontend/CLAUDE.md](frontend/CLAUDE.md), [cli/CLAUDE.md](cli/CLAUDE.md).

## Commands

### Full stack
```bash
ollama pull qwen3:8b && ollama pull qwen2.5-coder:7b   # any two models; nothing is pinned
ollama pull embeddinggemma                   # optional: semantic retrieval instead of word overlap
docker compose up --build -d                 # backend :8000, frontend :3000
docker compose --profile redis up -d         # optional shared cache/queue

SANDBOX_TIER=core docker compose up --build -d   # smaller sandbox image
EXECUTION_BACKEND=host docker compose up -d      # no sandbox image at all
```

## Conventions

Conventional commits are enforced by commitlint via pre-commit (`pre-commit install --hook-type commit-msg`).
