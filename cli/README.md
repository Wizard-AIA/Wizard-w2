# wizard CLI

A single static binary that manages the Wizard backend and frontend as a
background service — the same subcommands, same behavior, on Linux, macOS
and Windows. It replaces the manual `uvicorn` + `pnpm dev`/`start` dance
(and `docker compose up`, which stays available and opt-in per Milestone 3)
with `wizard init && wizard start`.

See the root [`CLAUDE.md`](../CLAUDE.md) for how this fits into the rest of
the architecture. This file is build instructions and a subcommand
reference.

## Building

Requires Go 1.23+.

```bash
cd cli
go build -o wizard ./cmd/wizard          # wizard.exe on Windows
```

To stamp the binary with a specific backend API compat version (see
`internal/compat/version.go`) rather than the literal compiled into the
source:

```bash
go build -ldflags "-X wizard/internal/compat.CompatAPIVersion=4.0.0" -o wizard ./cmd/wizard
```

Match that value to `API_VERSION` in
`backend/src/api/routes/meta.py` — `wizard start` refuses to run against a
backend whose major version doesn't match this binary's.

### Cross-compiling

Go cross-compiles from any one machine with no target toolchain install —
just `GOOS`/`GOARCH`:

```bash
CGO_ENABLED=0 GOOS=linux   GOARCH=amd64 go build -o dist/wizard-linux-amd64     ./cmd/wizard
CGO_ENABLED=0 GOOS=linux   GOARCH=arm64 go build -o dist/wizard-linux-arm64     ./cmd/wizard
CGO_ENABLED=0 GOOS=darwin  GOARCH=amd64 go build -o dist/wizard-darwin-amd64    ./cmd/wizard
CGO_ENABLED=0 GOOS=darwin  GOARCH=arm64 go build -o dist/wizard-darwin-arm64    ./cmd/wizard
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -o dist/wizard-windows-amd64.exe ./cmd/wizard
```

`CGO_ENABLED=0` gives a true static binary on every target — no shared libc
dependency at runtime, no target sysroot needed to build. There is
deliberately no release pipeline publishing these yet (Milestone 8 scopes
that out — see the "binary self-update" note below); building from source is
the documented way to get the binary for now.

### Running the tests

```bash
go test ./...
```

Everything above spawns no process and dials nothing — matching the
backend's own "tests never touch a real process" rule. One integration
test (in `internal/daemon`) actually spawns real child processes to verify
the supervisor's spawn/health-poll/restart/clean-stop behavior; it's gated
behind an env var so the default run stays fast:

```bash
WIZARD_CLI_SELFTEST=1 go test ./internal/daemon/... -run TestSupervisor -v
```

## Subcommands

Installed release binaries work from any directory — the installers persist
the bundled checkout in `WIZARD_ROOT`, and the CLI also resolves the stable
`current` package link. When running a source-built binary, use it from inside
a Wizard checkout (or set `WIZARD_ROOT` yourself).

| Command | What it does |
|---|---|
| `wizard init` | In a terminal, a bare run interactively configures provider, data mode, schema-only privacy, manager/worker models, embeddings, and the relevant endpoint/API key; press Enter to keep defaults and use ↑/↓ plus Enter for choices. It then checks Python 3.12+/Node 20+/uv/pnpm and only requires Ollama when the selected configuration needs a local model server. Missing required tools trigger an arrow-key confirmation and can be installed through winget, Homebrew, apt, dnf, pacman, or apk; `--install-prerequisites` opts in for scripts and `--no-install-prerequisites` only checks. It copies `backend/.env.example` → `backend/.env` if missing; creates a venv under the platform config directory with `uv venv` and installs backend requirements with `uv pip install`; runs `pnpm install --frozen-lockfile && pnpm run build` for the frontend's production `standalone` bundle. `--pull-models` also `ollama pull`s a default manager/worker pair if Ollama is present. Reads host RAM and, if the default manager+worker pair clearly won't fit together and neither `--manager-model` nor `--worker-model` was given, pins one smaller model for both roles instead in the `.env` it creates (announced, not silent — see Design notes) — an explicit `--manager-model`/`--worker-model` is always respected as-is. `--provider`/`--data-mode`/key flags configure a local, hybrid or fully cloud setup in the same run — see [Local, hybrid and cloud setups](#local-hybrid-and-cloud-setups) below. `--non-interactive` disables prompts for automation; `--interactive` forces them. |
| `wizard start` | Re-execs itself into a detached background supervisor (backend + frontend), waits here in the foreground until the backend answers healthy, checks the backend's reported API version against this binary's compat marker, then opens a browser. `--backend-port`/`--frontend-port` override the 8000/3000 defaults; `--no-browser` skips opening one. |
| `wizard stop` | Idempotent. Asks the supervisor to stop and waits for it to clean up; falls back to a forced kill of the recorded pids if it doesn't. |
| `wizard delete` | Stops Wizard and deletes its user-level config, credentials, connections, skills, logs, and managed venv, plus `backend/.env`. The checkout and CLI remain installed. Prompts for confirmation; use `--yes` for automation or `--keep-env` to preserve the checkout's `.env`. |
| `wizard status` / `wizard doctor` | Same command (the spec lists them as one thing). Local checks (what's running, log sizes, `API_PROVIDER`/`DATA_MODE`, `EXECUTION_BACKEND`) plus, when the backend answers, a render of its own `GET /api/config` — host sizing, sandbox capability, performance notes and the rest already live there; this reuses it rather than re-deriving anything. |
| `wizard attach` | Prints status, then follows `backend.log`/`frontend.log` live, source-prefixed, until Ctrl+C. Read-only. |
| `wizard logs` | One-shot: prints the log file paths; `--tail N` also prints the last N lines of each. |
| `wizard update` | `git pull --ff-only`, reinstalls dependencies (the same steps as `init`), re-checks the compat marker. Restarts the daemon afterward if it was running before. Scoped to the checkout only this milestone — see below. |
| `wizard skills add/list/update/discard/remove/token` | Fronts `backend/main.py skills` — the same install machinery (fetch, pin to a commit, show every skill's full contents, ask before writing) the REST routes and web UI's install-from-GitHub flow use, now also reachable from the compiled binary. Runs in the wizard-managed venv from `wizard init`; `add`/`update` prompt on a real terminal unless `--yes` is given. |
| `wizard version` | Prints this binary's compiled-in compat version. |

### Local, hybrid and cloud setups

Wizard is local-first, not local-only: `wizard init` sets up a plain Ollama
install by default, but the same command also configures a hybrid or fully
cloud install in one run, rather than leaving that to a hand-edit of
`backend/.env` afterward.

When run without configuration flags from a terminal, `wizard init` starts a
setup questionnaire. It covers the settings most users need on first launch:
provider, data mode, schema-only cloud sharing, manager/worker models,
embedding provider/model, and the selected provider's API key and endpoint.
Existing values are shown as defaults and are kept by pressing Enter. Use
`wizard init --non-interactive` in scripts; the existing flags remain available
for one-command configuration, and `--interactive` explicitly forces the
questionnaire.

```bash
# Local (default) -- nothing to add.
wizard init

# Fully cloud: Anthropic for both roles, no local weights needed.
wizard init --provider anthropic --anthropic-key sk-ant-...
# --data-mode is left empty here on purpose: it derives to cloud-only once
# API_PROVIDER is a cloud backend (see backend/.env.example) -- nothing to
# pass unless you want to say so explicitly.

# OpenAI instead:
wizard init --provider openai --openai-key sk-...

# Gemini, via its OpenAI-compatible endpoint:
wizard init --provider gemini --gemini-key AI...

# Any other OpenAI-compatible endpoint -- Groq, OpenRouter, Together, vLLM:
wizard init --provider custom_gateway --gateway-url https://api.groq.com/openai/v1 --gateway-key gsk_...

# Hybrid: keep the local Ollama pair init would otherwise pick, and also
# make a cloud key available for whichever role you assign to it from the
# Models page later.
wizard init --data-mode hybrid --anthropic-key sk-ant-...

# Point a provider at a proxy instead of its official endpoint.
wizard init --provider openai --openai-key sk-... --base-url https://my-proxy.example/v1
```

A few things this does for you beyond writing the flag values into
`backend/.env`:

- A **pure-cloud** setup (`--provider` is `anthropic`/`openai`/`gemini`/
  `custom_gateway` and `--data-mode` is not `hybrid`) skips the RAM-based
  Ollama manager/worker sizing entirely — there is no local model to size —
  and leaves `MODEL_NAME`/`WORKER_MODEL_NAME` empty (auto-select on that
  provider) unless you pin one yourself.
- Any provider other than plain `ollama` — including `lmstudio` — needs
  `langchain-openai` (Anthropic needs `langchain-anthropic` too), which is
  not in the base `requirements.txt`/`requirements-local.txt` install.
  `wizard init`/`wizard update` read `API_PROVIDER`/`DATA_MODE` back out of
  `backend/.env` and add `requirements-optional.txt` to the install
  automatically when one of them needs it, so a cloud/hybrid setup ends up
  with a working client, not an `ImportError` on the first turn.
- A cloud provider missing its key (e.g. `--provider anthropic` with no
  `--anthropic-key`) does not fail the run — it prints where to add the key
  (`wizard init --anthropic-key ...` again, a hand-edit of `backend/.env`,
  or the Models page once the app is running), the same detect-and-instruct
  approach `wizard init` already takes for a missing Python/Node/Ollama.
- Every flag here is also safe to run again against an **already
  configured** `backend/.env` — unlike the passive RAM-based model pick
  (which only ever applies to a freshly created file), an explicit
  `--provider`/`--data-mode`/key flag is a deliberate instruction and always
  takes effect, so `wizard init --data-mode cloud-only` is how you flip an
  existing local install over later. It only ever writes the fields you
  named; an existing key is never blanked by omitting its flag.
- Nothing here changes where generated code actually runs — that is
  `EXECUTION_BACKEND`/`SANDBOX_*`, an orthogonal axis `wizard init` never
  touches with these flags. A cloud provider only changes where the
  reasoning/code-writing model calls go.

## What's deliberately out of scope this milestone

- **Binary self-update.** `wizard update` updates the backend/frontend
  checkout only. Updating the `wizard` binary itself via GitHub Releases
  needs a release pipeline (build matrix, checksums, a way to fetch and
  atomically replace a running binary) that doesn't exist yet. Flagged as a
  follow-up, not silently dropped.
- **Unattended system changes.** A bare interactive `wizard init` asks before
  installing missing prerequisites. Scripts must explicitly pass
  `--install-prerequisites`; use `--no-install-prerequisites` to disable all
  installation. Package availability still depends on the host OS and its
  configured package manager.
- **Owning the Docker daemon's lifecycle.** `wizard start`/`doctor` only
  probe reachability and pass `EXECUTION_BACKEND` through — an unreachable
  Docker under `docker` mode still degrades to `host` the way the backend
  already handles it (see `core/tools/runtime.py`).
- **Remote access.** The daemon binds to `127.0.0.1` only, on both the
  backend and frontend sides. No tailscale-style remote reach is planned —
  confirmed out of scope by the evolution spec.

## Design notes

- **Config directory**: `internal/appdir` is a Go port of
  `backend/src/utils/appdirs.py`'s `config_dir()` — same
  `WIZARD_CONFIG_DIR` override, same per-platform default
  (`%APPDATA%\Wizard`, `~/Library/Application Support/Wizard`,
  `$XDG_CONFIG_HOME/wizard`). Kept in lockstep by hand since a Go process
  can't import the Python module. Milestone 8 adds `run/` (pid files, the
  stop sentinel, the crash marker), `logs/` and `venv/` under it, beside the
  `credentials.json`/`connections.json`/`skills/` earlier milestones
  already put there.
- **RAM-aware model default**: `internal/hostinfo` is a Go port of the RAM
  figure `backend/src/utils/hostinfo.py` reads (not its core-count detection
  or laptop/server/hpc classification — those feed `LLM_NUM_THREAD` sizing,
  an axis `wizard init` never touches). `internal/commands/modelfit.go`
  mirrors `resources.py`'s `estimate_footprint` fallback branch and
  `DEFAULT_MEMORY_FRACTION` closely enough to reach the same fits/doesn't-fit
  call on a given host, without a real Ollama registry lookup to size
  against — `wizard init` runs before one is reachable. It only overrides a
  model the user didn't already name; see the `wizard init` row above.
- **Provider/data-mode config** (`internal/commands/provider.go`): mirrors
  `backend/src/providers.py`'s provider table (kept in lockstep by hand, the
  same as `internal/appdir`) closely enough to validate `--provider`/
  `--data-mode` and know which `backend/.env` key each flag targets, without
  asking the not-yet-running backend. `applyProviderConfig` writes only the
  fields a flag actually named, against a fresh or pre-existing
  `backend/.env` alike — a deliberate contrast with `ensureEnvFile`'s
  passive, fresh-file-only RAM-based model pick just above it.
  `needsOptionalRequirements` (`deps.go`) reads `API_PROVIDER`/`DATA_MODE`
  back out of `backend/.env` rather than being told, so `wizard update`
  reinstalls correctly with no state of its own to keep in sync with `init`.
- **Process supervision** (`internal/daemon`): `wizard start` re-execs
  itself into a detached, hidden `__supervise` subcommand so the
  supervision loop survives the `start` command returning. The supervisor
  polls the backend's `GET /health` and does a bare TCP check against the
  frontend port (which has no equivalent health route), restarting either
  child with capped exponential backoff on failure and writing a `crashed`
  marker — rather than continuing to retry forever — once the restart
  budget is exhausted.
- **Cross-platform process control** lives in `process_unix.go`/
  `process_windows.go` (Go's build-tag-by-filename convention, matching the
  split `backend/src/core/security/sandbox/` already uses for the same
  reason). POSIX: process groups, `SIGTERM` then `SIGKILL`. Windows attempts
  `CTRL_BREAK_EVENT` first (the same technique
  `backend/src/core/tools/host_runtime.py` uses to interrupt one execution),
  but `GenerateConsoleCtrlEvent` only reaches a process sharing a console
  with the caller, and the detached, consoleless `__supervise` process this
  package actually runs as never does — so in the shipped binary the real
  stop path is `taskkill /T /F` once the grace period elapses, every time,
  not "usually graceful."
- **Version compatibility**: no new backend field — `internal/compat`
  compares only the *major* component of the existing `API_VERSION`
  (`backend/src/api/routes/meta.py`, reported by both `/health` and
  `/api/config`) against this binary's build-time compat marker, so a
  routine backend patch/minor bump doesn't force a CLI rebuild.
