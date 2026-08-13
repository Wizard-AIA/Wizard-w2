# mac_migrations.md — context handoff to a fresh Claude Code session on macOS

**Read this once, act on §1, then delete it via §9.** It is a one-time brain transfer from a
Windows 11 Claude Code session (last active 2026-08-13) to a fresh install on a new MacBook. It
carries the things a `git clone` does *not*: user working preferences, machine-local state that
will not survive the move, the true status of in-flight work, and the platform assumptions that
change when the primary dev machine stops being Windows.

Everything the repo already records — architecture, invariants, commands, test policy — lives in
`CLAUDE.md`, `backend/CLAUDE.md`, `frontend/CLAUDE.md`, `cli/CLAUDE.md` and `docs/`. This file
does **not** duplicate those. It points at them and covers only what they cannot.

---

## 1. First action on the Mac: write these memories, then delete this file

Create each file below under the memory directory for this project
(`~/.claude/projects/<project-slug>/memory/`), then add the one-line pointers to `MEMORY.md`.
The three marked **(carried over)** are verbatim from the Windows session's memory and must not
be lost. The rest are facts that were live in the old session's context but were never written to
disk — they are the actual reason this handoff file exists.

### 1.1 `no-long-foreground-runs.md` (carried over)

```markdown
---
name: no-long-foreground-runs
description: Never start blocking multi-minute model or Docker workloads in a session; ship reviewed code and hermetic tests instead.
metadata:
  type: feedback
---

Do not start live, blocking multi-minute model or Docker workloads inside a Claude Code
session. Deliver reviewed code and hermetic tests instead; hand the user a runnable harness
for anything that genuinely needs live inference.

**Why:** The old dev machine was a 4-core / 15.7 GB laptop where a single local-model turn ran
tens of seconds and a benchmark sweep ran hours, blocking the session for no reviewable output.
The constraint is about session discipline, not raw hardware, so it survives the move to faster
hardware — a fast Mac makes live runs cheaper, not more appropriate to block a session on.

**How to apply:** Build the harness, verify it hermetically, then tell the user the exact command
to run separately and ask for the results back as data. `scripts/benchmark_harness/` is the
worked example of this pattern. See [benchmark-remediation-phase-2-pending].
```

### 1.2 `comment-style.md` (carried over)

```markdown
---
name: comment-style
description: Single-line comments only where necessary; keep docstrings concise.
metadata:
  type: feedback
---

Use single-line comments, and only where they earn their place. Keep docstrings concise.

**Why:** The user maintains this codebase solo and treats redundant narration of what the code
already says as noise to be reviewed and deleted.

**How to apply:** Comment the non-obvious *why*, never the *what*. Match the density of the
surrounding file. No block-comment banners, no docstring for a self-evident helper.
```

### 1.3 `wizard-manager-worker-split.md` (carried over)

```markdown
---
name: wizard-manager-worker-split
description: Wizard splits the LLM into a manager (MODEL_NAME) that plans and a worker (WORKER_MODEL_NAME) that writes code.
metadata:
  type: project
---

In Wizard, `MODEL_NAME` is the **manager**: it orients, decides the next action each loop
iteration, and synthesises the final answer — three to five calls per turn. `WORKER_MODEL_NAME`
is the **worker**: it writes the code that gets executed. They resolve independently, and either
can be local or cloud subject to the session's data mode.

**Why:** The roles have opposite requirements. The manager makes many short decisions and must
follow instructions and answer directly; the worker makes few long generations. Pairing a
reasoning-distill model to the manager role was measured as pathological — it emitted a multi-
thousand-token `<think>` block to pick one word from a three-item menu.

**How to apply:** When touching model selection, budgets, or provider resolution, ask which role
is affected — they are never a single knob. `settings.output_budget(purpose)` and
`settings.budget_for(mode, parameter_size)` are where the two axes combine.
See [wizard-benchmark-report-was-falsified].
```

### 1.4 `wizard-repo-name-mismatch.md` (new — write this)

```markdown
---
name: wizard-repo-name-mismatch
description: The Wizard checkout directory is named w1 but the project and GitHub repo are w2.
metadata:
  type: project
---

The local checkout on the old machine was at `C:\3rd_Year\Wizard-w1`, but the project is
**Wizard w2** and the remote is `https://github.com/Wizard-AIA/Wizard-w2.git`. The directory
name is stale, not a different project. w1 is the previous-generation codebase that w2 evolved
from; `docs/wizard-w1-to-w2-migration.md` records that transition.

**Why:** The repo was migrated from a personal GitHub account to the `Wizard-AIA` organization
and renamed `Wizard-w1` → `Wizard-w2` (see CHANGELOG `[Unreleased]`), but the working copy was
never re-cloned.

**How to apply:** On the Mac, clone fresh as `Wizard-w2` so the path stops lying. Treat any
"w1" reference in paths as the old checkout and any in prose as the genuinely older codebase.
```

### 1.5 `wizard-benchmark-report-was-falsified.md` (new — write this)

```markdown
---
name: wizard-benchmark-report-was-falsified
description: Wizard's w2 benchmark report was contradicted by its own raw data; the corrected findings live in the remediation plan.
metadata:
  type: project
---

`docs/wizard_w2_full_benchmark_report.md` was originally produced by a Google Antigravity IDE
session and its headline numbers were false in its own source data — a grading pipeline stamped
`one_shot_success: true` on turns whose recorded output was the model narrating a `KeyError`.
The real local-mode count was 3 genuine LLM successes, not "13/13, 100%". The report has since
been corrected in place; `docs/benchmark-report-remediation-plan.md` Phase -1 is the audit trail.

**Why:** This is the single most load-bearing correction in the project's history and it is not
recoverable from the code. The raw evidence lived in machine-local Antigravity state on the old
Windows laptop and is **gone** with that machine — Phase -1 is the extracted record, not a
pointer to re-fetch.

**How to apply:** Never grade an agent run from a summary field the harness computed itself;
grade from the actual answer/stdout text. That rule is implemented in
`scripts/benchmark_harness/grading.py` and is non-negotiable for any future benchmark work.
Treat any pre-correction figure quoted elsewhere as suspect.
See [benchmark-remediation-phase-2-pending].
```

### 1.6 `benchmark-remediation-phase-2-pending.md` (new — write this)

```markdown
---
name: benchmark-remediation-phase-2-pending
description: Every benchmark remediation phase is done except the full-turn re-run, which the user must execute.
metadata:
  type: project
---

In `docs/benchmark-report-remediation-plan.md`, Phases 0, 1, 2.1, 2.3, 2.4, 3.1 and 3.2 are
**Done**. The only outstanding item is **Phase 2.2 — the full-turn re-run**: the harness is
built at `scripts/benchmark_harness/run_benchmark.py` but has not been executed, because it
needs live model inference.

**Why:** It is gated on the standing no-live-inference-in-session rule, not on missing code.
It is the project's one open loop as of 2026-08-13.

**How to apply:** Do not silently run it to "finish" the plan. Hand the user the command and
ask for `scripts/benchmark_harness/results/*.json` back. See [no-long-foreground-runs].
```

### 1.7 `wizard-sandbox-macos-now-primary.md` (new — write this)

```markdown
---
name: wizard-sandbox-macos-now-primary
description: With the move to a MacBook, Wizard's macOS sandbox-exec path becomes the daily-driver backend; Windows job objects become untestable locally.
metadata:
  type: project
---

Wizard's OS-native sandbox has three implementations (`backend/src/core/security/sandbox/`):
Landlock+seccomp on Linux, an SBPL profile via `sandbox-exec` on macOS, a job object plus Low
integrity level on Windows. Development moved from Windows to macOS on/around 2026-08-13, so
the **macOS path is now the one exercised locally** and the Windows path is CI-only.

**Why:** The macOS SBPL profile has a long tail of syntax-sensitive fixes behind it (network
bind rules, file-vs-dir rules, `ipc-posix-shm`, symlink handling, an unsupported-`sandbox-exec`
fallback) and `sandbox-exec` is availability-**probed**, never inferred from OS version — it has
carried a deprecation warning since 10.14 and still works. Regressions there will now show up as
broken local execution rather than as a CI-only failure.

**How to apply:** Changes to Windows sandbox code can no longer be smoke-tested locally — lean on
`backend/tests/` and CI, and say so rather than implying local verification. Sandbox self-tests
under `backend/tests/sandbox/` spawn a process and are skipped unless `WIZARD_SANDBOX_SELFTEST=1`.
See [wizard-repo-name-mismatch].
```

### 1.8 `MEMORY.md` index lines to append

```
- [No Long Foreground Runs](no-long-foreground-runs.md) — never block a session on live model or Docker work.
- [Comment Style](comment-style.md) — single-line comments only where necessary; concise docstrings.
- [Manager/Worker Split](wizard-manager-worker-split.md) — MODEL_NAME plans, WORKER_MODEL_NAME writes code.
- [Repo Name Mismatch](wizard-repo-name-mismatch.md) — directory says w1, project and remote say w2.
- [Benchmark Report Was Falsified](wizard-benchmark-report-was-falsified.md) — the w2 report contradicted its own raw data.
- [Benchmark Phase 2 Pending](benchmark-remediation-phase-2-pending.md) — the full-turn re-run is the one open loop.
- [macOS Sandbox Now Primary](wizard-sandbox-macos-now-primary.md) — sandbox-exec is the daily driver now.
```

---

## 2. Who you are working with

Aniket Saha (`aniketsahaworkspace@gmail.com`), sole maintainer of Wizard, working on it as a
third-year project (`3rd_Year/` in the old path). Git author name on all commits: `Aniket Saha`.

Working style observed across the Windows sessions, worth matching:

- **Conventional commits, enforced.** commitlint runs via pre-commit on `commit-msg`. Run
  `pre-commit install --hook-type commit-msg` on the new machine or commits will pass locally
  and fail in CI.
- **Small, single-purpose branches**, named `fix/<thing>` or `milestone-N-<name>`, merged by PR.
  There are ~15 stale local `fix/*` branches on the old machine; a fresh clone drops them, which
  is the desired outcome — all of them are merged or abandoned.
- **Documentation is a deliverable, not an afterthought.** `docs/` has 15 files and the
  `CLAUDE.md` set is layered so folder guides load only when work touches that folder. Keep that
  discipline: architecture rationale goes in `docs/`, invariants go in the folder `CLAUDE.md`,
  never both.
- **Honest reporting is the standing expectation.** The single largest piece of work in this
  repo's recent history was auditing and correcting a benchmark report that overstated its own
  results. Do not round a partial result up to a pass.

---

## 3. What the project is, in one screen

**Wizard w2** — a local-first autonomous data analysis agent.

A FastAPI backend orchestrates a **manager** model (reasons, plans, answers) and a **worker**
model (writes code), each resolvable to a local provider (Ollama, LM Studio) or a cloud provider
(Anthropic, OpenAI, a gateway) under an explicit, session-wide **data mode**
(`local-only` / `cloud-only` / `hybrid`). Generated Python runs in a per-session sandbox — a host
subprocess under OS-native containment by default, or a Docker container if you opt in — and
streams reasoning, code, stdout and the final answer to a Next.js client over one WebSocket.

Monorepo:

| Path | Stack | Role |
|---|---|---|
| `backend/` | Python 3.11, FastAPI | Orchestrator, execution, sandbox, providers, skills, connectors |
| `frontend/` | Next.js 16 / React 19 / Tailwind v4 | Five routes, no landing page — `/` *is* the workspace |
| `cli/` | Go 1.23, static binary `wizard` | Manages backend+frontend as a background service |
| `docs/` | Markdown | Design rationale, 15 files, indexed by `docs/architecture.md` |
| `scripts/benchmark_harness/` | Python | The measurement harness (see §6) |

The w2 line was scoped as **ten milestones** (CHANGELOG `[v2.0.0-w2-planning]`, 2026-08-07):
provider-agnostic model layer → depth/permission dials → host-primary execution with OS
sandboxing → connectors → skills → skill registry → subagents → the `wizard` Go CLI → re-runnable
export → versioning and release polish. All ten have landed code; work since has been hardening,
security audit follow-ups, CI/CD and the benchmark correction.

**Read next, in this order, when you start real work:** root `CLAUDE.md` → the folder `CLAUDE.md`
for wherever you are working → `docs/architecture.md` → the specific `docs/*.md` it points you to.

---

## 4. State of play as of 2026-08-13

Branch `master`, clean tree, 139 commits since 2026-08-01. Most recent:

```
a95d478 fix(sandbox): bind the selftest probe into its Windows job object (#132)
5d7876b fix(backend): serve plot.html inline with a sandbox CSP, not as an attachment (#131)
701b7b7 Merge pull request #130 ... fix/low-effort-issue-cleanup-2
c051467 fix: openapi-typescript codegen and route-layer DI for singletons
efd1814 feat(cli): front backend/main.py's skills subcommands from wizard
9d1ece3 fix(backend): bound RelationalConnector.fetch() by CONNECTOR_MAX_ROWS
```

The immediately preceding stretch (`8b9f52e` back through `139a77b`) is a long run of
`fix(security)` commits against issue #120, almost all of them **macOS SBPL profile syntax** —
network bind rules, IP filtering, file-vs-dir rules, `ipc-posix-shm`, an invalid `sysctl*`
directive, symlink support, and finally a fallback for when `sandbox-exec` is unsupported. That
work was done blind, on Windows, without a Mac to test on. **You now have the Mac.** Verifying
that path end-to-end is the highest-value thing the new hardware unlocks — see §5.

### Open issues (6)

| # | Title | Labels |
|---|---|---|
| 117 | [Feat-3] Smart Tiered Model Routing (Manager vs Worker LLM Dispatcher) | enhancement, performance, architecture, llm |
| 116 | [Feat-2] Dynamic Skill & Tool RAG retrieval to keep system prompts under 1,500 tokens | enhancement, performance, skills, llm |
| 115 | [Feat-1] Semantic Result Cache to bypass LLM API calls on recurring questions | enhancement, performance, llm |
| 114 | [Perf-2] Zero-Copy Apache Arrow streaming between backend and frontend | enhancement, frontend, performance |
| 113 | [Perf-1] Integrate DuckDB & Polars in execution backend for 10x–50x faster queries | enhancement, performance |
| 104 | Security: OpenSSF Scorecard remediation (reach 9.5+) | dependencies, area:ci-cd, security |

113–117 were all filed 2026-08-09 as a batch and are the declared forward roadmap. 104 is
grind-it-out CI hardening.

### Open PRs (4) — all Dependabot, all stale-ish

`#52` python-dependencies (32 updates), `#51` langchain 0.2.17 → 1.3.9, `#50` frontend deps
(58 updates), `#47` `golang.org/x/sys` 0.27.0 → 0.47.0. The langchain major bump is the only one
that is not routine; the frontend group is large enough to want a real `pnpm build` + `tsc` pass
behind it. Note the lock files are hash-pinned `uv pip compile` output — do not hand-edit them.

---

## 5. What actually changes on macOS

This is the section that justifies the file. Everything here is a Windows assumption that either
breaks, flips, or stops being locally verifiable.

### 5.1 The sandbox flips from job objects to SBPL

`backend/src/core/security/sandbox/` splits per-OS by build-tag-style filenames
(`windows.py`, plus `policy.py` / `profiles.py` / `capability.py` / `selftest.py` shared).

- **Was (Windows):** a job object supplying `ProcessMemoryLimit`, `ActiveProcessLimit` and
  `KILL_ON_JOB_CLOSE`, plus a Low integrity level the child applies to itself, with the workspace
  labelled Low via `icacls`.
- **Now (macOS):** a deny-by-default SBPL profile through `sandbox-exec`. Availability is
  **probed** by actually running a restrictive profile, never inferred from OS version.
  `sandbox-exec` has been formally deprecated since 10.14 and still works; the documented
  replacement (App Sandbox entitlements) needs a signed app bundle, which this project does not
  have. There is a fallback for the unsupported case (`8b9f52e`).

Run the sandbox self-tests early on the Mac — they spawn a real process and are **skipped by
default**, including in CI:

```bash
WIZARD_SANDBOX_SELFTEST=1 pytest backend/tests/sandbox -q
```

If those pass on real hardware, a large amount of blind-fix work from issue #120 is confirmed.
If they fail, that is the first real signal anyone has had. `docs/sandbox.md` has the full
two-phase enforcement model.

### 5.2 Windows-specific code is now CI-only

Several things in the codebase exist purely for Windows and can no longer be smoke-tested
locally. Do not claim local verification of any of them:

- `cli/internal/.../process_windows.go` — the `taskkill /T /F` stop path. Note the documented
  reality in `cli/CLAUDE.md`: `CTRL_BREAK_EVENT` is attempted but never actually reaches the
  detached consoleless supervisor, so in the shipped binary the stop path is `taskkill`
  **every time**, not "usually graceful."
- `core/tools/host_runtime.py`'s `CTRL_BREAK_EVENT` interrupt.
- Credential file permissions: POSIX `0600` vs the Windows SID-based ACL path (which resolves the
  user via `whoami /user`, deliberately not `%USERNAME%`). **On macOS you are now on the `0600`
  branch**, which is the simpler and better-tested one.
- `CodeGuard`'s drive-letter path handling (`_is_path_allowed` folds backslashes and checks
  `X:/`). Still must not regress.
- The daemon script interpolates paths with `%r`, not into `"..."`, specifically because of
  Windows escape sequences. Keep that even though it now matters less locally.

### 5.3 Toolchain and commands

Shell changes from PowerShell to zsh — every command in the `CLAUDE.md` files was already written
POSIX-first, so they should work as-is. What to install:

```bash
# Toolchains
# Python 3.11, Node (for pnpm@10.33.3), Go 1.23
brew install go node ollama
npm i -g pnpm@10.33.3          # or corepack enable
pip install uv                  # backend installs use uv, not pip

# Repo — clone as Wizard-w2, not Wizard-w1
git clone https://github.com/Wizard-AIA/Wizard-w2.git && cd Wizard-w2
pre-commit install --hook-type commit-msg     # commitlint; skipping this breaks CI later

# Backend
uv pip install --system -r requirements.txt          # API server only
uv pip install --system -r requirements-local.txt    # analysis toolkit, Docker-less
uv pip install --system -r requirements-optional.txt # Redis / OpenAI gateway

# Frontend
cd frontend && pnpm install

# Models — nothing is pinned; any two will do
ollama pull qwen3:8b && ollama pull qwen2.5-coder:7b
ollama pull embeddinggemma      # optional: semantic retrieval instead of word overlap
```

Tests run **from the repo root** (`pyproject.toml` sets `testpaths` and `pythonpath`):
`pytest`. Full suite was 1256 passed / 6 skipped in ~134s on the old 4-core laptop; expect
substantially faster on Apple silicon. `asyncio_mode = "auto"`, so async tests need no decorator.

Frontend CI gates, all three: `pnpm lint && npx tsc --noEmit && pnpm build`.

### 5.4 Hardware assumptions are now stale — in a good way

The old machine was measured at **4 physical cores, 15.7 GB RAM**, and that measurement is baked
into commentary in `backend/.env` explaining why `LLM_NUM_THREAD` and `LLM_NUM_CTX` are left
unset (they derive to 4 threads / 8192 context from the measured host). `SYSTEM_PROFILE=auto`
means the host is measured at boot and derivation fills only unset fields — **so this self-corrects
on the Mac with no action needed.** Do not hand-set those values; the whole point of the
derivation is that it adapts. Just be aware that any absolute latency figure quoted anywhere in
`docs/` describes the old, memory-constrained laptop.

Also: unified memory changes what model pairs fit. `docs/benchmark-report-remediation-plan.md`
§3.1 found that **2 of the 5 recommended model pairs land in swap** against the codebase's own
memory-planning arithmetic — on the old machine. That finding is hardware-dependent and worth
re-deriving via `scripts/benchmark_harness/validate_model_pairs.py`, which needs no live
inference.

---

## 6. Local state that does NOT survive the move

Assume all of this is gone unless you deliberately copy it. Listed roughly by how much it hurts.

1. **`backend/.env` and root `.env`** — gitignored, so not in the clone. Between them they hold
   `MODEL_TYPE`, `MODEL_NAME`, `WORKER_MODEL_NAME`, `VISION_MODEL_NAME`, `API_PROVIDER=ollama`,
   `GATEWAY_API_URL`, `MAX_TOKENS`, `TEMPERATURE`, `PLOT_FORMAT`, `SANDBOX_NETWORK_DISABLED`,
   `SANDBOX_DOCKER_RUNTIME`, `SYSTEM_PROFILE`, plus **two API keys** (`GEMINI_API_KEY` at root,
   `GATEWAY_API_KEY` in `backend/.env`). The keys are not reproduced here on purpose — re-issue
   or copy them by hand. Ask the user rather than guessing values.
   `backend/.env` also carries long explanatory comments that are genuinely load-bearing
   documentation about the manager/worker choice and the thread/context derivation; if you can
   copy one file across, copy this one.
2. **Stored credentials** — `credentials.json` / `connections.json` / `skills/` live in the
   platform config dir (`backend/src/utils/appdirs.py`, ported to Go in `cli/internal/appdir`,
   kept in lockstep by hand). On macOS that path differs from Windows. Credentials are **never
   returned by any route** — only `has_key` and masked hints — so they cannot be exported through
   the app. Re-enter them.
3. **`workspace/`** — session state plus scratch datasets (`dataset.csv`, `dataset.feather`,
   `df.csv`, `housing.csv`, `student-dataset.csv`, `sessions/`, two backend log dumps). The
   benchmark suite references the student dataset (real columns:
   `id, name, nationality, city, latitude, longitude, gender, age, englishgrade, mathgrade,
   ethnicgroup, ...`). If any benchmark re-run is planned, **copy `workspace/student-dataset.csv`
   across** or Phase 2.2 is not reproducible.
4. **Docker images** — `wizard-sandbox:standard` (1.47 GB) was built and present. Rebuild with
   `docker compose up --build -d`, or `SANDBOX_TIER=core` for the smaller image, or skip Docker
   entirely with `EXECUTION_BACKEND=host`. Note `runtime.TIER_MODULES` mirrors the Dockerfile and
   a test asserts they agree.
5. **The Antigravity evidence archive** — the raw JSON behind the falsified benchmark report was
   at `C:\Users\zenbook duo\.gemini\antigravity-ide\brain\ef58ee7e-.../scratch/`. It is **gone**.
   `docs/benchmark-report-remediation-plan.md` Phase -1 is the extracted record and is now the
   only surviving account. Treat it as primary, not as a pointer.
6. **`.claude/settings.local.json` permission allowlist** — the old session had pre-approved
   `ruff check/format`, the two pytest invocations, and a couple of `python -c` forms. It is
   committed at `.claude/settings.local.json`, but the Windows-shaped `Read(//c/Users/...)` entry
   is dead on macOS. Rewrite that entry or drop it. Consider `/fewer-permission-prompts` once you
   have a few sessions of history on the new machine.
7. **`.coverage`, `.pytest_cache`, `.ruff_cache`, `.hypothesis`, `.benchmarks`, `logs/`** —
   disposable, regenerate on first run.

---

## 7. Where the work goes next

In rough priority order, as the old session understood it:

1. **Verify the macOS sandbox on real hardware** (§5.1). Highest value-per-hour available, and it
   is the one thing only the new machine can do. Issue #120's fix run was authored blind.
2. **Phase 2.2 — the full-turn benchmark re-run.** The harness is built and waiting at
   `scripts/benchmark_harness/run_benchmark.py`; everything else in the remediation plan is done.
   This is the project's single open loop. Per §1.1 this is the **user's** command to run, not a
   session's — hand it over and take the JSON back. The non-negotiable rule baked into
   `grading.py`: grade from actual answer/stdout content, never from a harness-computed summary
   field. That exact anti-pattern produced both false "13/13" and false "10/10" in the original.
3. **The five roadmap issues (113–117)** — semantic result cache, skill/tool RAG retrieval under
   1,500 tokens, tiered manager/worker routing, DuckDB+Polars in the execution backend, zero-copy
   Arrow streaming. All performance/architecture, all filed together, none started.
4. **Issue #104 — OpenSSF Scorecard to 9.5+.** Mechanical CI hardening.
5. **The four Dependabot PRs**, with real gate runs behind the langchain and frontend ones.

---

## 8. Hard-won invariants worth carrying in your head

Not a substitute for the folder `CLAUDE.md` files — these are the ones that were most often
nearly broken, so they are the ones worth knowing before you open a file.

- **`CodeExecutor.execute` is the only path generated code reaches an interpreter.** Three
  backends: `host` (default), `docker` (opt-in, degrades to host if unreachable), `inprocess`
  (tests/dev only).
- **Any path to generated code must come from `runtime.workspace_path(session_id, name)`.** A
  literal `/workspace` only exists inside containers.
- **`POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`.** The transport
  translates events to frames and must contain no workflow logic.
- **It is a loop, not a pipeline.** Each iteration the manager sees what actually ran and picks
  the next move. `parse_decision` never raises.
- **Data mode is enforced in exactly one place** — `LLMProvider.resolve` — because that is the
  one function every LLM call site passes through. `local-only` *refuses* cloud providers; that
  is a hard boundary, not a preference. Data mode outranks permission profile.
- **`_extract_code` strips reasoning first.** A model drafts code inside `<think>`, discards it,
  then writes the real thing — searching the raw response runs the rejected draft.
- **A skill may never carry executable code**, enforced in `loader.load_skill`. **Skill install is
  user-initiated only, never an agent action** — that is the anti-worm rule.
- **Tests never touch Docker, Ollama or the network, and never spawn a process.**
  `backend/tests/conftest.py` pins the env *before* importing `src`, because Settings instantiates
  at import time. `EXECUTION_BACKEND=inprocess` is load-bearing there —
  `SANDBOX_ENABLED=false` alone only means "no Docker".
- **A live-inference bug hid behind a permissive test default once already.** `conftest.py` pins
  `DATA_MODE=hybrid`, which masked a missing `data_mode=` argument on the worker call site in
  `_generate()` — every hybrid/cloud-only session on a stock install refused to write code. Fixed
  at `orchestrator.py:1669`. Be suspicious of test defaults that are more permissive than reality.
- **`usage` events are emitted only when a cloud model ran.** Under `local-only`, silence is the
  honest surface.
- **Frontend is light-only, tokens-only.** No `dark:` utilities, no raw hex, no arbitrary duration
  classes. Every socket handler must check `socketRef.current === socket` or StrictMode remounts
  leak orphan sockets.

---

---

## 9. Self-destruct — run this when the handoff is done

**This file is committed to git**, so deleting it from disk is not enough — it has to come out of
the repo too, or the next clone resurrects it.

**Do not run this until §1 is actually complete.** The bar is: every memory file in §1 exists on
disk under the project memory directory, `MEMORY.md` carries all seven pointer lines, and you have
read enough of §5 to know the sandbox story. Verify before deleting — this file is the only copy
of most of what it holds, and nothing else in the repo records it.

```bash
# 0. Verify the memories landed first — expect 7 files + MEMORY.md
ls ~/.claude/projects/*/memory/
cat ~/.claude/projects/*/memory/MEMORY.md

# 1. Remove it from the working tree and the index in one step
git rm mac_migrations.md

# 2. Commit the removal
git commit -m "chore: remove mac_migrations handoff file after context transfer"

# 3. Push, so no other clone drags it back
git push
```

If for any reason it was never committed on this machine, `rm mac_migrations.md` alone is enough
and steps 1–3 do not apply.

**Instruction to the assistant, stated plainly:** once the §1 memories are written and verified,
run the sequence above without waiting to be asked again. This file is scaffolding with a defined
end of life — leaving it in the repo is the failure mode, not deleting it. Do not archive it, do
not move it to `docs/`, and do not rewrite history to purge it (a plain `git rm` commit is
correct; the content is not sensitive — no keys or credentials are reproduced in it).

---

*Written 2026-08-13 on Windows 11, for a fresh Claude Code session on macOS. Once §1 is done,
run §9 — the memories are the load-bearing part, this file is not.*
