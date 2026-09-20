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

Requires the Go version declared in `go.mod`.

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

### Cross-compiling and releases

Go cross-compiles from any one machine with no target toolchain install, just
`GOOS`/`GOARCH`. Releases are built by `scripts/build_release.sh`, which reads
the target matrix from `packaging/release.json` (the one place the supported
OS/architecture pairs are defined, and which `internal/platform`'s test checks
against) and builds each with `CGO_ENABLED=0 -trimpath -buildvcs=false` for a
static, reproducible binary:

```bash
scripts/build_release.sh            # builds v<VERSION> for every target into dist/
```

Supported targets: `darwin-arm64`, `darwin-amd64`, `linux-amd64`, `linux-arm64`,
`windows-amd64`. Windows on ARM is not built or tested, and the installers say
so instead of downloading an archive that does not exist. The version comes from
the repository's `VERSION` file (`scripts/release.py check` keeps every copy in
step) and is stamped into the binary with `-X wizard/internal/compat.BuildVersion`.

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

Installed binaries work from any directory: the CLI locates the bundled
`backend/` and `frontend/` from its own resolved path (a Homebrew keg's
`libexec`, the installer's `current` package, or a Scoop app directory), so no
environment variable is needed. When running a source-built binary, use it from
inside a Wizard checkout or set `WIZARD_ROOT`. Global flags go before the
command: `--no-color` (also `NO_COLOR`) and `--verbose`.

| Command | What it does |
|---|---|
| `wizard init` | Sets Wizard up: chooses a provider and models, checks (and offers to install) prerequisites, writes `backend/.env`, creates a venv under the config directory with `uv`, installs backend requirements, and builds the frontend's production `standalone` bundle. On a terminal it is interactive (see [Interactive setup](#interactive-setup)); `--non-interactive` never prompts and `--interactive` forces prompts. Prerequisites follow the [version policy](#prerequisites) below; `--install-prerequisites` opts in for scripts and `--no-install-prerequisites` only checks. It only requires Ollama when the chosen configuration needs a local model server. `--pull-models` also `ollama pull`s a default manager/worker pair. Reads host RAM and, if the default pair clearly won't fit and neither `--manager-model` nor `--worker-model` was given, pins one smaller model for both roles in the `.env` it creates (announced, not silent); an explicit model flag is always respected. `--provider`/`--data-mode`/key flags configure a local, hybrid or cloud setup in the same run, see [Local, hybrid and cloud setups](#local-hybrid-and-cloud-setups). After configuring it keeps a private copy of `backend/.env` in the config directory and restores it if the package directory is later replaced (a `brew upgrade`, a reinstall). |
| `wizard start` | Re-execs itself into a detached background supervisor (backend + frontend), waits until the backend answers healthy, checks the backend's reported API version against this binary's compat marker, then opens a browser. `--backend-port`/`--frontend-port` (validated as 1-65535) override the 8000/3000 defaults; `--timeout` bounds the wait; `--no-browser` skips opening one. Tools outside the shell's PATH (a keg-only Homebrew formula, a user-level pnpm) are added to the supervisor's PATH after your own. |
| `wizard stop` | Idempotent. Asks the supervisor to stop and waits for it to clean up; falls back to a forced kill of the recorded pids if it doesn't. |
| `wizard delete` | Stops Wizard and deletes its user-level config, credentials, connections, skills, logs, and managed venv, plus `backend/.env`. The checkout and CLI remain installed. Prompts for confirmation; use `--yes` for automation or `--keep-env` to preserve the checkout's `.env`. |
| `wizard status` | What's running, log sizes, `API_PROVIDER`/`DATA_MODE`/`EXECUTION_BACKEND`, and, when the backend answers, a render of its own `GET /api/config` (host sizing, sandbox capability, performance notes). |
| `wizard doctor` | A read-only health report of this installation with PASS / WARN / FAIL per check and a fix for anything that is not a pass: version, platform, install method, whether `wizard` is on PATH, the bundled files, the config directory, Python / Node / uv / pnpm (a tool that is present but cannot run is a failure, with the reason), configuration and credentials, the running service and its API compatibility. It works when the bundled files cannot be found, which is when it is needed. `--json` for scripts, `--network` to also test GitHub Releases and the configured model provider. Exits 3 if a check fails. |
| `wizard update` | Updates a release install: checks GitHub Releases, verifies the archive's `SHA256SUMS` entry, stages the full package, preserves `backend/.env`, rebuilds it, then advances `current` while keeping the previous package. A git checkout uses `git pull --ff-only`. It **never overwrites files a package manager owns**: on Homebrew or Scoop it prints `brew upgrade wizard` / `scoop update wizard`. A failed update restarts the service it stopped. `--check` only reports (safe on any install). |
| `wizard uninstall` | Removes what the official installer created: the install directory's known contents (never `RemoveAll` on the directory itself), the shell startup blocks or fish drop-in, and on Windows the user PATH entry and `WIZARD_ROOT`. It keeps your data and backs up `backend/.env` to the config directory. **`--purge`** (alias `--all`) removes everything: program, config, API keys, logs and the managed Python environment, and lists exactly what it will delete before asking. Refuses git checkouts and unrecognised layouts, and on Homebrew/Scoop prints the package manager's own command. `--yes` skips the prompt; unattended runs without it are refused. |
| `wizard attach` | Prints status, then follows `backend.log`/`frontend.log` live, source-prefixed, until Ctrl+C. Read-only. |
| `wizard logs` | One-shot: prints the log file paths; `--tail N` also prints the last N lines of each. |
| `wizard skills add/list/update/discard/remove/token` | Fronts `backend/main.py skills` — the same install machinery (fetch, pin to a commit, show every skill's full contents, ask before writing) the REST routes and web UI's install-from-GitHub flow use, now also reachable from the compiled binary. Runs in the wizard-managed venv from `wizard init`; `add`/`update` prompt on a real terminal unless `--yes` is given. |
| `wizard version` | Prints this binary's immutable release build version and compiled-in backend API compatibility marker. It performs no network check; use `wizard update --check` to see whether a newer release is available. |

### Interactive setup

On a terminal a bare `wizard init` is a guided flow:

- **Menus are real dropdowns** where the terminal supports ANSI cursor control:
  the highlighted row is marked, long lists scroll, typing jumps to a match,
  ↑/↓, PgUp/PgDn, Home/End and Enter work, and a finished choice collapses to one
  line. Terminals without cursor control (dumb terminals, old consoles) fall
  back to a numbered list, and `NO_COLOR` only turns colour off.
- **API keys** show one `•` per typed or pasted character, then a receipt
  (length and last four characters). The key itself is never printed, and it is
  checked against the provider immediately; a rejected key offers to re-enter it.
- **Models come from the provider.** Once credentials are known, `init` asks the
  provider what it offers (Ollama's installed models, LM Studio's, and the
  OpenAI, Anthropic, Gemini or gateway model lists) and offers only usable chat
  or embedding models, stable releases before previews. Image, speech, music and
  specialist models are hidden. Nobody is asked to type a model name or a
  provider URL: endpoints are known, and if a list cannot be fetched the choice
  stays on auto-select with a pointer to the Models page. A local server on
  another machine is asked for only when the default address is unreachable.
- **Hybrid** setups also pair a cloud provider and verify its key.

Scripted and piped use is unchanged: no menus, no network lookups, typed model
names as before.

### Prerequisites

Python 3.12+, Node.js 20+, uv and pnpm are **minimums, not pins**.

1. Anything at or above the minimum that is already on the machine is used as
   is, wherever it lives: `PATH`, Homebrew (including keg-only formulae), user
   bins, and the nvm, fnm, Volta, asdf and mise directories (searched after
   `PATH`, so your own choice always wins).
2. Only what is missing is installed, and the current release is preferred
   (unversioned, so it is linked onto PATH), falling back to the minimum-version
   package. Python is provisioned with `uv python install` on Linux and Windows.
3. A tool that exists but cannot run is reported as broken rather than missing.
   Tools that only a shell can start (pnpm 12's shebang-less placeholder) are run
   through `sh` instead of failing with `exec format error`.

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

## Exit codes and environment

| Code | Meaning |
|---|---|
| 0 | success (including `--help`) |
| 1 | failure |
| 2 | bad usage or an invalid value (a stray argument, port outside 1-65535) |
| 3 | missing dependency or broken installation; run `wizard doctor` |
| 4 | network error (release lookup, download) |

| Variable | Effect |
|---|---|
| `WIZARD_ROOT` | directory containing `backend/` and `frontend/` (only needed for a source build) |
| `WIZARD_CONFIG_DIR` | overrides the settings/credentials/logs/venv location |
| `NO_COLOR`, `--no-color` | disable colour (`--no-color` and `--verbose` go before the command: `wizard --no-color doctor`) |
| `WIZARD_ASCII=1` | ASCII symbols only |
| `WIZARD_VERBOSE`, `--verbose` | show underlying error detail |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | honoured by the updater, downloads and model lookups |
| `GITHUB_TOKEN`, `GH_TOKEN` | optional; sent to `api.github.com` only, to lift the anonymous rate limit on `wizard update` |

## Release updates and version awareness (in scope)

The tagged-release workflow is the delivery channel for the compiled CLI.
Release-installed binaries use this flow today; source checkouts continue to
use their explicit Git update path:

- `wizard update --check` makes an explicit, bounded release query and reports
  the installed build version, available version, installation channel, and
  whether an update is applicable. Ordinary CLI invocations must not make a
  silent network request.
- Release binaries are stamped with an immutable build version and each release
  publishes a `SHA256SUMS` integrity file. The updater rejects a missing,
  malformed, duplicate, incompatible, or mismatched checksum before unpacking.
- `wizard update` automatically selects the matching OS/architecture archive
  for a recognized release installation; `wizard update --self` selects this
  path explicitly. It stages the full package, preserves `backend/.env`,
  rebuilds it before activation, then safely switches the launcher/current
  pointer, and retains the previous package for rollback. Windows uses a
  detached helper so it never overwrites the running executable in place.
- Package managers own their installs. `internal/installkind` recognises a
  Homebrew keg, a Scoop app, the official installer's layout and a git
  checkout from the paths alone; `update` and `uninstall` refuse to touch the
  first two and print the package manager's command.
- Source and binary updates remain deliberately separate. An ordinary source
  checkout uses Git; a release install uses verified packages. Neither updates
  the active release when staging or preparation fails.

## What's deliberately out of scope this milestone

- **Unattended system changes.** A bare interactive `wizard init` asks before
  installing missing prerequisites. Scripts must explicitly pass
  `--install-prerequisites`; use `--no-install-prerequisites` to disable all
  installation. Package availability still depends on the host OS and its
  configured package manager.
- **Owning the Docker daemon's lifecycle.** `wizard start`/`doctor` only
  probe reachability and pass `EXECUTION_BACKEND` through — when Docker is
  unavailable, the backend selects its supported in-process fallback (see
  `core/tools/runtime.py`).
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
