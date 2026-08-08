# Contributing to Wizard w2 🧙‍♂️

Thanks for considering a contribution. This guide covers what you need to know to get productive quickly and to get a PR merged without surprises.

---

## Architecture in one minute

Wizard is a **local-first** agent. Read [CLAUDE.md](./CLAUDE.md) for the full tour; the short version:

1. **Manager model** — reasons about the request and produces a plan, then writes the final answer from the real execution output.
2. **Worker model** — turns the plan into Python. Nothing else.
3. **Code guard** — one AST policy check, in [`backend/src/core/security/code_guard.py`](backend/src/core/security/code_guard.py). It is the only authority on whether generated code may run.
4. **Runtime** — one Docker container *or* one subprocess per session, created lazily. Both run the same daemon; `EXECUTION_BACKEND` chooses.

Two rules that are easy to violate by accident:

- **There is one workflow implementation.** `AnalysisOrchestrator.run` is it. The REST and WebSocket handlers only translate events into frames. If you find yourself adding step-sequencing logic to a transport, it belongs in the orchestrator — that split is exactly what drifted before and caused features to work on one path only.
- **Everything degrades.** No Docker, no embedding model, no Redis and no model server are all supported states. New dependencies must be optional or gracefully absent.
- **Weight is a feature.** This runs on laptops. A dependency that adds gigabytes needs to justify them: `sentence-transformers` was removed because torch pulls ~2.8 GB of CUDA wheels on Linux to run a 90 MB model. Check what a package actually installs before pinning it.

> [!IMPORTANT]
> Keep the local-first default intact. Cloud providers are supported through `API_PROVIDER`, but nothing may *require* an external service to run.

---

## Setup

**Prerequisites:** Python 3.11+, Node.js 20+, Ollama. Docker is optional and opt-in — the default `EXECUTION_BACKEND=host` runs generated code in a subprocess of the backend.

```bash
git clone https://github.com/YOUR_USERNAME/Wizard-w2.git
cd Wizard-w2
git remote add upstream https://github.com/Wizard-AIA/Wizard-w2.git

# Any two models. Nothing in the app is tied to a particular one — an empty
# MODEL_NAME means "use whatever this provider has installed".
ollama pull qwen2.5-coder:7b
ollama pull qwen3:8b

uv pip install --system -r requirements.txt        # API server
uv pip install --system -r requirements-local.txt  # + analysis toolkit, if you run without Docker
cd frontend && pnpm install && cd ..

pre-commit install --hook-type commit-msg   # conventional commits are enforced
```

Copy [backend/.env.example](backend/.env.example) to `backend/.env` if you want to change defaults; the app runs without it.

---

## Running things

```bash
# Backend (from backend/)
uvicorn src.api.api:app --reload --port 8000

# Frontend (from frontend/)
pnpm dev

# CLI, same stack
python backend/main.py path/to/data.csv
```

---

## Quality gates

These are exactly what CI runs. Run them before pushing.

**Backend**
```bash
ruff check . --fix
ruff format .
pytest                    # from the repo root
```

**Frontend**
```bash
cd frontend
pnpm lint                 # errors only; warnings are allowed
npx tsc --noEmit
pnpm build
```

---

## Tests

Four layers under `backend/tests/`:

| Layer | What belongs there |
|-------|--------------------|
| `unit/` | One module, no I/O |
| `integration/` | Real app, real SQLite, stubbed LLM |
| `regression/` | A specific past defect, with a docstring saying what broke |
| `negative/` | Hostile, malformed and degenerate input |

**The suite must never need Docker, a model server, the network, or a spawned process.** `backend/tests/conftest.py` pins `EXECUTION_BACKEND=inprocess`, `SANDBOX_ENABLED=false` and `EMBEDDINGS_FORCE_FALLBACK=true` *before* importing `src`, because `Settings` is built at import time. If you add a test that needs a real service, mark it `@pytest.mark.requires_docker` or `@pytest.mark.requires_llm`.

`EXECUTION_BACKEND=inprocess` matters: `SANDBOX_ENABLED=false` alone now means only "no Docker", and the default `host` would spawn a child that imports pandas for every session the suite creates.

When you fix a bug, add a regression test whose docstring explains the original failure. Anyone can write `assert x == y`; the value is in recording why it was ever `z`.

---

## Making changes

**Branches:** `feature/…`, `fix/…`, `docs/…`, `refactor/…`, `test/…`

**Commits:** conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). commitlint runs as a pre-commit hook and also gates PRs.

**Style:**
- Python — Ruff is the only linter and formatter. Line length is 120; `E501` is off because the formatter owns wrapping.
- TypeScript — strict mode, no `any`. `react-hooks/set-state-in-effect` is an **error**: do not call `setState` synchronously in an effect body, and do not silence the rule.
- Tailwind v4 only, through the design tokens in `globals.css`. Do not hardcode colours — everything must work in light and dark.

**Comments** should explain *why*, especially where the obvious approach was rejected. Several modules carry short notes about a previous implementation and the failure it caused; keep that habit rather than deleting the context.

---

## Pull requests

1. Sync with `upstream/master`.
2. Make sure all gates above pass locally.
3. Fill in the PR template. For UI changes, attach a screenshot or a short clip.
4. One maintainer approval plus green CI is required to merge.

Touching security-sensitive code — the code guard, the sandbox, session handling, or anything that executes generated code — needs negative tests demonstrating the new boundary holds.

---

## Reporting security issues

Please do **not** open a public issue. See [SECURITY.md](./SECURITY.md). Priority areas: sandbox escape, code-guard bypass, cross-session data access, and endpoint injection.

---

## Community

Search existing issues before opening a new one, and include a reproducible case. Use GitHub Discussions for design questions and RFCs.
