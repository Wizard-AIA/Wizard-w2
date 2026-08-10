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

Run from inside a Wizard checkout (or any subdirectory of one) — `wizard`
locates the checkout root by walking up looking for `backend/main.py` +
`frontend/package.json`, the way `git`/`pnpm` locate their own project root.

| Command | What it does |
|---|---|
| `wizard init` | Checks Python 3.11+/Node 20+/uv/pnpm (and optional Ollama) are on PATH; copies `backend/.env.example` → `backend/.env` if missing; creates a venv under the platform config directory with `uv venv` and installs backend requirements with `uv pip install`; runs `pnpm install --frozen-lockfile && pnpm run build` for the frontend's production `standalone` bundle. `--pull-models` also `ollama pull`s a default manager/worker pair if Ollama is present. Detects and instructs — it never invokes a package manager to install Python/Node/Ollama/uv/pnpm themselves. Reads host RAM and, if the default manager+worker pair clearly won't fit together and neither `--manager-model` nor `--worker-model` was given, pins one smaller model for both roles instead in the `.env` it creates (announced, not silent — see Design notes) — an explicit `--manager-model`/`--worker-model` is always respected as-is. |
| `wizard start` | Re-execs itself into a detached background supervisor (backend + frontend), waits here in the foreground until the backend answers healthy, checks the backend's reported API version against this binary's compat marker, then opens a browser. `--backend-port`/`--frontend-port` override the 8000/3000 defaults; `--no-browser` skips opening one. |
| `wizard stop` | Idempotent. Asks the supervisor to stop and waits for it to clean up; falls back to a forced kill of the recorded pids if it doesn't. |
| `wizard status` / `wizard doctor` | Same command (the spec lists them as one thing). Local checks (what's running, log sizes, `EXECUTION_BACKEND`) plus, when the backend answers, a render of its own `GET /api/config` — host sizing, sandbox capability, performance notes and the rest already live there; this reuses it rather than re-deriving anything. |
| `wizard attach` | Prints status, then follows `backend.log`/`frontend.log` live, source-prefixed, until Ctrl+C. Read-only. |
| `wizard logs` | One-shot: prints the log file paths; `--tail N` also prints the last N lines of each. |
| `wizard update` | `git pull --ff-only`, reinstalls dependencies (the same steps as `init`), re-checks the compat marker. Restarts the daemon afterward if it was running before. Scoped to the checkout only this milestone — see below. |
| `wizard skills add/list/update/discard/remove/token` | Fronts `backend/main.py skills` — the same install machinery (fetch, pin to a commit, show every skill's full contents, ask before writing) the REST routes and web UI's install-from-GitHub flow use, now also reachable from the compiled binary. Runs in the wizard-managed venv from `wizard init`; `add`/`update` prompt on a real terminal unless `--yes` is given. |
| `wizard version` | Prints this binary's compiled-in compat version. |

## What's deliberately out of scope this milestone

- **Binary self-update.** `wizard update` updates the backend/frontend
  checkout only. Updating the `wizard` binary itself via GitHub Releases
  needs a release pipeline (build matrix, checksums, a way to fetch and
  atomically replace a running binary) that doesn't exist yet. Flagged as a
  follow-up, not silently dropped.
- **Auto-installing Python/Node/uv/pnpm/Ollama.** `wizard init` detects and
  prints the right install command for the host OS; it never runs a package
  manager on the user's behalf for these.
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
