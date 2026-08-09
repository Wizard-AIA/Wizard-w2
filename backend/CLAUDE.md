# Backend

Moved out of the root `CLAUDE.md` so it loads only when work touches `backend/`. The root file is the always-loaded one; everything here is backend-specific architecture, commands and test policy.

## Commands

### Without Docker
```bash
uv pip install --system -r requirements.txt -r requirements-local.txt
uvicorn src.api.api:app --port 8000          # from backend/; EXECUTION_BACKEND defaults to
                                             # host, which runs code in a subprocess
```

### Backend
```bash
uv pip install --system -r requirements.txt              # root file, not backend/ — API server only
uv pip install --system -r requirements-local.txt         # the analysis toolkit, for running without Docker
uv pip install --system -r requirements-optional.txt      # only for Redis or an OpenAI gateway

uvicorn src.api.api:app --reload --port 8000 # run from backend/
python backend/main.py path/to/data.csv      # CLI REPL over the same stack

pytest                                       # from repo ROOT (pyproject sets testpaths/pythonpath)
pytest backend/tests/unit -q                 # one layer
pytest backend/tests/unit/test_code_guard.py::test_repair_strips_markdown_fences

ruff check . --fix && ruff format .          # CI runs `ruff check` + `ruff format --check`
```

`asyncio_mode = "auto"`, so async tests need no decorator.

**Tests never touch Docker, Ollama or the network, and never spawn a process.** `backend/tests/conftest.py` sets `EXECUTION_BACKEND=inprocess`, `SANDBOX_ENABLED=false`, `HOST_SANDBOX=off` and `EMBEDDINGS_FORCE_FALLBACK=true` *before* importing `src`, because `Settings` is instantiated at import time. Keep new env pinning at the top of that file.

An autouse fixture also stubs `tools.packages.install`. Consent to a library install is acted on in the parent, which put a real `pip install` directly behind a gate several tests approve on purpose — the suite reached PyPI for 600 seconds once before that stub existed. Stubbed rather than pinned off through settings, because that setting also decides whether the gate is *offered*, so turning it off silences the consent tests instead of protecting them.

`backend/tests/sandbox/` is the one directory that spawns a process. It is skipped unless `WIZARD_SANDBOX_SELFTEST=1` is set, and CI never sets it; it is the only place the containment claims are checked against a kernel rather than a docstring.

`EXECUTION_BACKEND=inprocess` is load-bearing: `SANDBOX_ENABLED=false` alone now means only "no Docker", and the default `host` would spawn a child that imports pandas for every session the suite creates.

## Architecture

### One request path

`POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`. The transport only translates events into frames — it contains no workflow logic. (Historically the WebSocket handler re-implemented the node loop by hand, and the two copies drifted until the semantic cache and the fast-path router applied to REST only.)

### Event protocol — [events.py](backend/src/core/agent/events.py)

The orchestrator knows nothing about WebSockets; it emits typed events to an `Emitter`. `EventCollector` buffers them for the REST path and for tests; `WebSocketEmitter` serialises them onto a socket.

Frames: `session`, `status`, `step_start`/`step_end`, `reasoning_delta`, `plan_delta`, `content_delta`, `code`, `stdout`, `artifact`, `approval_required`, `warning`, `error`, `final`. `approval_required` carries an `id` **only** for a mid-run permission gate — its presence tells the client the turn is paused rather than ended, and is what distinguishes a reply from a new turn.

Investigation frames, added with the agentic loop: `iteration_start`, `action`, `observation`, `finding`, `plan_revised`, `assumption`, `verification`. `skill` names which skill informed the turn — emitted rather than left implicit in a prompt nobody sees, because "the agent can name which skill informed a decision" is a Milestone 5 acceptance criterion and a frame is the only way it is true on screen. `skill_candidate` carries an offer to save an analysis worth keeping, and nothing else; the file appears only if the user confirms. Both `recurring` (the same question asked again) and `recovery` (a failure-then-fix that recurred) candidates emit this same frame type, distinguished by the candidate's `kind`. `usage` carries what the turn cost, and is emitted **only when a cloud model ran** — under `local-only` there is nothing to meter and silence is the honest surface. The frames above are all still emitted, so a client that ignores these degrades rather than breaking. `observation` closes the most recent `action` that has none — the backend never correlates them by id.

`reasoning_delta` vs `plan_delta` are split *during* streaming by tracking the `<thought>` tag boundary incrementally ([`_stream_plan`](backend/src/core/agent/orchestrator.py)), so the UI can show a live thinking panel that switches to the plan at the right moment.

### Workflow — [orchestrator.py](backend/src/core/agent/orchestrator.py)

**This is a loop, not a pipeline.** Each iteration the manager sees what has actually run and chooses the next move; the run ends when it says it can answer, or the budget is spent.

```
orient (plan) → [plan gate] → loop → verify → answer
                                ↑ ↓
                 inspect / code+execute / consult / reflect
                          ↓         ↓
              [permission gate]  correct   (bounded by MAX_CORRECTION_RETRIES)
```

The two gates are deliberately distinct: the plan gate ends its turn and is resumed by starting a new one, while a permission gate **suspends** and resumes in place. See the permission-profile section.

- The old shape fixed a plan before touching the data and fed 200 characters of each step's output into the next. It could not recover when the data contradicted the plan. [DABstep](https://arxiv.org/abs/2506.23719) measures the gap: hard tasks need 6+ dependent steps, the best model scores 14.55% on them vs 76.39% on single-step ones, and planning is the largest error category.
- Actions live in [actions.py](backend/src/core/agent/actions.py). `parse_decision` **never raises** — malformed model output resolves to a default (`code` mid-run, forced `answer` on the last iteration). The loop is worthless if a small model saying `**Action:** Code.` derails it.
- `inspect` is answered deterministically from the frame (`Session.inspect`), costing no LLM call. That is what makes it worth offering as an action.
- Budgets come from `settings.budget_for(mode, parameter_size)` — see the config section. Every iteration is a manager round-trip.
- Modes: `auto` (agent picks its depth), `fast` (one shot, **and no verification** — it is the most expensive thing a turn can do), `deep`. `planning` is a legacy alias meaning "deep, but gate the plan".
- **Below the balanced tier the loop decides for itself** (`TierBudget.allow_decisions=False`). Asking a 1.5B model to choose an action costs a round-trip and buys nothing: it reads the transcript and picks from three options it does not reliably distinguish, and a reasoning distill spends its whole output budget deliberating and returns nothing parseable — so the call is paid for and the default is taken anyway. `_decide_deterministically` reads the answer off what happened: **a step that succeeded and printed something means stop**, anything else means write code. That turns a nine-call compact turn into a three-call one. `deep` restores the round-trip on every tier, or the composer's Deep control would be a no-op on exactly the setup where someone reaches for it.
- The same function is the *default* on every tier when the model's answer is unparseable. The old default was `code` unconditionally, so the failure mode of asking a weak model to choose was to keep spending.
- `AGENT_TURN_TIMEOUT` is a wall-clock deadline. It is checked **before an iteration is claimed** — never mid-call, since a request in flight is already paid for, and not after `iterations_used` is incremented, or the turn reports an iteration it abandoned. Verification is the first thing it gives up.
- Plan approval is opt-in (`AGENT_REQUIRE_APPROVAL`). A plan containing `SEARCH: "…"` halts for consent regardless — that leaves the machine. Under `local-only` it is **refused** rather than gated: there is no consent that would make it allowed, so asking would be theatre.
- An approved plan skips `_orient` entirely, which is where the gate lives, so it cannot re-fire. It must also not be downgraded to `fast`: approving work is not asking for less of it.
- **The final answer is synthesised by the manager from real execution output** (`create_answer_prompt`). Do not reintroduce client-side cleanup of the response — the frontend used to regex-strip tracebacks, code blocks and numeric rows out of it, which deleted legitimate results.

### Subagents — [orchestrator.py](backend/src/core/agent/orchestrator.py)

`parallel` (Milestone 7) is a fourth kind of step alongside `code`/`consult`/`reflect`: the manager fans one step out into several concurrent, isolated mini-investigations instead of running them one at a time through the main loop. Offered only when `settings.SUBAGENT_ENABLED and budget.allow_subagents and budget.max_subagents >= 2` — the compact tier never sees it, for the same reason it never sees a decision round-trip.

- **Real concurrency needs one runtime per branch.** The daemon protocol is single-in-flight per process — one `accept()` loop, one shared `exec_globals`, a process-global stdout swap (`tools/daemon.py`) — so multiplexing several branches' calls onto one session's own daemon was never an option. `Session.spawn_subagent_id` mints a composite id (`f"{session.id}{CHILD_DELIMITER}{branch}"`) that gets its own runtime, workspace and usage-ledger bucket for free, because every place that already keys off a session id (execution, the guard's extra roots, `usage_ledger`) keys off *this* id instead when a subagent is running.
- **`SubagentSession` is a structural proxy around the parent `Session`, not a subclass.** It overrides only `.id`, `.executor` (a fresh `CodeExecutor(child_id)`) and `.workspace` (so two branches writing `plot.html` don't race on the parent's copy); everything else — `.df`, `.tables`, `.permissions`, `.data_mode`, `.models`, `.catalog`, `.has_documents` — forwards to the parent through `__getattr__`. A subagent investigates the same data under the same policy and the same permission grants; it just runs in its own process. The accepted consequence: concurrent branches share one `PermissionState`, so two branches can both reach `consent_broker.ask` for the same subject before either calls `.grant`, producing two prompts for one thing. Known and accepted for this milestone rather than chased as a bug.
- **`_split_subgoals` splits on `\s+\|\s+`, not a bare `|`.** A goal like "count rows where status matches A|B" would otherwise read as two sub-goals instead of one. Fewer than two real sub-goals degrades to a plain `_act_code` step rather than spawning one pointless subagent or failing the turn — the same "everything degrades" rule as elsewhere.
- **The child id is qualified by `group` (`f"{group}-sub{index+1}"`), not just the branch label.** A second `parallel` decision later in the same turn reuses `"sub1"`; without the group prefix that would collide with the first branch's still-or-already-torn-down workspace and usage-ledger bucket.
- **Each branch runs a bounded, deterministic mini-loop** (`_run_subagent`, reusing `_act_code` verbatim against the proxy session) — no decision or verification round-trip inside it, since the *parent* verifies once at the end over everything folded back; `check_grounding`/`_verify` need no changes for that, because a subagent's `Step` is recorded into the parent's own `Investigation` like any other. A branch that fails or times out contributes nothing rather than a half-written step.
- **`inprocess` runs branches serially, not concurrently.** That backend has no per-call isolation — one process-global matplotlib/pyplot state among other things — so it is dev/test only, and adding locking to `execution.py` for a backend that never promised isolation would be effort spent on the wrong problem. The real backends (`host`, `docker`) run branches through `asyncio.gather` under a shared deadline (`SUBAGENT_TIMEOUT`, also clamped by whatever remains of `AGENT_TURN_TIMEOUT`); a branch that doesn't finish in time is cancelled and contributes nothing, exactly like a branch that raised.
- **`SUBAGENT_START`/`SUBAGENT_END` only bound a branch's lifetime for the UI.** Everything a branch does inside that window emits the *existing* frame types (`action`, `observation`, `code`, `stdout`, ...) additively tagged with `branch` by `BranchEmitter` — reusing them would be wrong for the top-level `action`/`observation` pair (whose "closes the most recent open entry" matching only holds under strict seriality) but is exactly right here, since each branch's own sequence is still strictly serial even though branches run concurrently with each other.
- **Cost rolls up automatically.** Every LLM call and execution a subagent makes books under its own composite id, so `usage_ledger.totals_many([session.id, *state.subagent_ids])` — not `totals(session.id)` alone — is what makes a turn's readout include subagent spend. `release_subagent_runtime` frees a finished branch's process/container immediately but deliberately does **not** forget its usage-ledger entry or drop it from `subagent_ids`, since `_finalize` still has to read that branch's cost after every branch has folded back; full teardown (`dispose_subagent`) waits for the whole turn, not just the branch, to end.

### Trust layer — [grounding.py](backend/src/core/agent/grounding.py)

Deterministic, no LLM calls, and it **reports rather than edits** — post-processing model output is exactly the mistake above.

- `check_grounding` flags numbers in the answer that appear in no execution output. Tolerance comes from the *answer's own* precision (`3.14` for an output of `3.14159` is reporting, not invention) plus magnitude words (`1.23 million`).
- `assumptions_from_code` reads silent decisions back out of the code that ran — `dropna`, `how='inner'`, `nlargest`, `errors='coerce'`. Each changes what the number means.
- `_verify` re-derives the headline result by a different route and looks for `VERIFIED:` / `MISMATCH:`. A wrong join grain produces a confident, plausible, wrong number that no self-review catches, because the reviewer is the model that made it.

### Execution — [execution.py](backend/src/core/execution.py) + [tools/runtime.py](backend/src/core/tools/runtime.py)

`CodeExecutor.execute` is the **only** way generated code reaches an interpreter. It guards first, then hands the code to whichever runtime `runtime.active_backend()` names. Semantic cleaning on upload goes through here too — but only when there is something to clean: `flow._needs_cleaning` looks for the three problems `create_cleaning_prompt` actually names (missing values, text that is really a number or a date, untrimmed whitespace) and skips the model entirely when it finds none. Every upload used to buy a worker round-trip and a sandbox execution to be told the data was fine, which on a local model is the slowest thing between choosing a file and seeing it appear. It is deliberately conservative — an unreadable column counts as "might need cleaning", since skipping a needed clean is a data bug while running an unneeded one only costs time.

**Two supported backends and one last resort**, selected by `EXECUTION_BACKEND`:

| Value | What runs the code | Isolation |
|-------|--------------------|-----------|
| `host` (default) | one **subprocess** per session | separate process with `RLIMIT_AS`, timeout, interrupt — **not yet** a security boundary |
| `docker` | one container per session | process, filesystem, network, memory, PIDs, caps |
| `inprocess` | guarded `exec` in the API process | none, and the namespace does not persist |

`auto` and `local` are the pre-w2 spellings and are folded to `host` by a `mode="before"` field validator, so an existing `.env` keeps working and nothing downstream ever sees them. **Docker is opt-in**: it is reached only when named, and naming it on a machine with no reachable daemon degrades to `host` with a warning. That reverses an earlier rule which resolved an unreachable Docker to `inprocess` so a weaker guarantee could not be substituted silently — but `inprocess` is the least contained runtime there is, so that answered "your container is missing" by removing the isolation that remained. The substitution is announced instead: logged, and `/settings` renders the setting and the resolved runtime separately.

`ExecutionResult.isolation` (`container` / `os-sandbox` / `process` / `none`) is what the UI keys on; `sandboxed` is derived from it. It used to mean "container specifically", which stopped being the same question once the host backend could be contained by the OS.

`local` is not a degraded mode and does not warn per message; only `inprocess` does. Docker remains the right answer for input you did not write yourself.

Both real backends run the **same daemon** over the same protocol — see [tools/daemon.py](backend/src/core/tools/daemon.py). That is what stops them drifting: the container preloaded every session table while the old in-process fallback rebuilt its globals on every call, so an investigation lost whatever iteration 1 computed — the one thing the agentic loop is built around.

`ExecutionResult.sandboxed` means **container specifically**; `ExecutionResult.backend` names which of the three actually ran it.

**Any path handed to generated code must come from `runtime.workspace_path(session_id, name)`.** A literal `/workspace` is only real inside a container; on the local backend it names a directory that does not exist, pandas raises `OSError`, and the caller reports a generic failure. That silently disabled semantic cleaning on upload and CSV export of a variable on *every* Docker-less install. It was invisible to the suite because the in-process backend CI uses happens to tolerate the same path, and it was found only by running the app — which is the argument for running it. `plot_output_path` and `prompts._workspace_root` are the same helper wearing different names; keep them delegating rather than re-deriving.

### Security — [code_guard.py](backend/src/core/security/code_guard.py)

One AST-based analyzer, not regex. It distinguishes:
- **policy violation** → stop and tell the user (`GuardVerdict.ok=False`)
- **syntax error** → retryable, feed back into the correction loop (`verdict.syntax_error`)

Blocks banned modules, banned builtins, interpreter-internals attributes, bare `__builtins__`, reflection with a computed or dunder attribute name, and literal file paths outside the writable roots. The container is the real boundary; this is defence in depth, and on the `local` backend it is the *only* static check there is.

`CodeGuard.scan(code, extra_roots=...)` — a local runtime works out of the session's own directory, not `/workspace`, and `CodeExecutor.guard` passes that in. Without it the guard rejects the very chart path the prompt handed the model. A drive-letter path is not `posixpath.isabs`, so `_is_path_allowed` folds backslashes and checks for `X:/` explicitly.

### The daemon — [tools/daemon.py](backend/src/core/tools/daemon.py)

`DAEMON_SCRIPT` is a string literal because the container receives it over `put_archive` into a stock Python image — edit execution semantics inside that string. Render it through `render_daemon()`; it is `%`-formatted, so **no bare `%` may appear in it**.

- Length-prefixed (`>I`) JSON over TCP. Actions: `execute`, `inspect_variables`, `reload_dataset`, `reset`, `capabilities`, `ping`.
- `df` is **not** passed per call; the daemon preloads it from `<workspace>/dataset.feather`. `Session._materialize` writes it; `reload_dataset` refreshes it without restarting the runtime.
- **Every** session table is preloaded into `tables['<table_key>']` from `<workspace>/tables/*.feather`, with `df` still bound to the active one. Cross-table questions need them all in the namespace at once. `remove_dataset` must call `reload_dataset()`, or the deleted frame stays queryable.
- `WORKSPACE` is parameterised: `/workspace` in a container, the session directory locally. Paths are interpolated with **`%r`**, not into `"..."`, because a Windows path inside a string literal is a set of escape sequences (`C:\Users` is a truncated `\U`). This previously used `Path.as_posix()`, which is only a *conversion on Windows* — on Linux and macOS a Windows path is one opaque filename, the backslashes survive, and the daemon fails to parse. That made two tests pass on the developer's machine and fail on every CI runner. `repr` is correct on every platform and keeps native separators, which is what the daemon wants since it runs on the same OS as the backend.
- `capabilities` probes with `find_spec`, so it costs a path search rather than twenty imports. It is consulted on every prompt build.
- `DaemonClient` owns the protocol; `SandboxSession` and `HostSession` add only lifecycle.

### Docker backend — [tools/sandbox.py](backend/src/core/tools/sandbox.py)

`SandboxPool` creates **one container per session**, lazily.

- The daemon records its own PID; `interrupt()` signals it directly. Signalling PID 1 would kill the container, since PID 1 is `sleep infinity`.
- Limits: `mem_limit`, `pids_limit`, optional `cpu_quota`, `cap_drop=ALL`, `no-new-privileges`, plus a socket deadline per execution. The daemon's own `RLIMIT_AS` is deliberately left off here — Docker already enforces the ceiling, and a soft limit inside a hard-limited cgroup turns an OOM kill into a confusing `MemoryError`.
- The image tag carries the tier (`settings.sandbox_image`), so switching `SANDBOX_TIER` builds a new image instead of reusing one with different libraries in it.

### OS sandbox — [security/sandbox/](backend/src/core/security/sandbox/)

With Docker opt-in, this is what stands between generated code and the machine on a default install. The AST guard is unchanged and still runs first; this is the layer beneath it.

`HOST_SANDBOX` is **`off` / `best-effort` / `require`**, defaulting to `best-effort`. Three states rather than a bool because a silent downgrade and a refusal are both wrong as a universal answer: a 5.10 kernel has no Landlock and must still be able to run, while someone who set `require` to get a boundary should not be handed a subprocess that merely looks like one. `HOST_SANDBOX_NETWORK` (`deny`/`allow`, default `deny`) governs *outbound* traffic only — loopback is always permitted, because the daemon protocol is a loopback socket.

Split so the majority is testable without spawning anything, which is the only way any of it gets reviewed from a developer machine running one OS:

| module | what it is |
|---|---|
| `policy.py` | `SandboxPolicy` — inert, JSON-safe data. The single description of the boundary; the three platforms are renderings of it. |
| `profiles.py` | Generates the macOS SBPL profile as a pure function, so it can be asserted as an exact string on Linux or Windows. |
| `capability.py` | What this machine can enforce, **per feature, with a reason for every gap**. |
| `spawn.py` | Decorates the launch. Returns a `SpawnPlan` rather than spawning, so `HostSession` stays the single owner of process lifecycle. |
| `child.py` | The only part that restricts a live process. Loaded **by file path**, imports nothing from `src`. |
| `selftest.py` | Spawns a probe that tries to escape and reports what stopped it. |

**Enforcement is two-phase**, because the daemon binds a loopback TCP listener and a filter denying `socket()` cannot be installed before it. `apply_policy` runs before the daemon (filesystem, memory, no-new-privs); `seal_network` runs after `listen()` — `accept()` on an already-bound descriptor makes no `socket()` call, so the connection the parent needs survives a filter refusing to create new ones. The bootstrap leaves the seal on `builtins.__wizard_seal__`; the daemon calls it and returns the result through `capabilities`, and `HostSession` logs that report at start, so **the parent reports what was enforced rather than what was configured**.

Both halves of that handoff go through **`import builtins`, never the `__builtins__` global**. `runpy.run_path` binds `__builtins__` in the daemon's globals to a *dict*, so `getattr` finds nothing on it: the seal was silently skipped on every real session while the self-test still reported the network blocked, because the probe calls the seal itself. A test pins the spelling.

- **Linux** — `PR_SET_NO_NEW_PRIVS`, then Landlock (syscalls 444/445/446), ABI probed via `create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` and the handled-access set masked to what that ABI admits, since passing an unknown bit fails the call outright. Then a seccomp-bpf filter refusing `socket()` for `AF_INET`/`AF_INET6`/`AF_PACKET`/`AF_NETLINK` — expressible exactly because the domain is a scalar argument, and BPF cannot dereference a pointer.
- **macOS** — a deny-by-default SBPL profile via `sandbox-exec`. Availability is **probed**, not inferred from an OS version: it has carried a deprecation warning since 10.14 and still works well past it, and the documented replacement (App Sandbox entitlements) needs a signed app bundle, which a `git clone` does not have.
- **Windows** — a job object supplying `ProcessMemoryLimit`, `ActiveProcessLimit` and `KILL_ON_JOB_CLOSE`. **This is what closes the `RLIMIT_AS`-on-POSIX-only gap.** Plus a Low integrity level the child applies **to itself** — Windows permits lowering your own token, and the alternative (`CreateProcessAsUserW`) would mean reimplementing `Popen`'s `poll`/`wait`/`terminate`/stdout pipe around a raw handle. Reads keep working under the no-write-up policy; the workspace is labelled Low via `icacls` so the child can still write there. Assignment to the job happens just after spawn rather than through `CREATE_SUSPENDED`, because `subprocess` closes the thread handle — a microsecond gap during interpreter startup, written down rather than glossed over. **Network is not enforced on Windows** and `capability.detect()` says so with the reason: WFP needs administrator, and AppContainer would require re-ACLing the user's Python installation.

**Two things that break without deliberate handling.** Matplotlib, fontconfig and pip cache under the user's home, which no writable root covers — so `policy.cache_environment()` redirects `MPLCONFIGDIR`, `XDG_CACHE_HOME`, `TMPDIR` and friends into the workspace, or the child dies on `import matplotlib` before any generated code exists. And a `workspace_write` grant from the permission profile widens the **sandbox** as well as the guard, or consent the user was asked for and gave reads as broken. A Landlock ruleset cannot be widened after `restrict_self`, and neither can an SBPL profile or a lowered token — so a grant made at iteration four goes through `runtime.rebind_roots`, which restarts the child. The daemon reloads the session's datasets and tables from the workspace, so what a restart costs is intermediate variables, not data.

**Verification is an action, not a reading.** `GET /api/sandbox/selftest` spawns a child through the real machinery and has it attempt each forbidden operation. Outcomes are `blocked` / `allowed` / **`inconclusive`** — the probe dials a TEST-NET address (RFC 5737, guaranteed not to route), so a timeout proves nothing either way, and calling that a pass would be exactly the invented claim the trust layer forbids. A feature the platform reports as unsupported does not fail the verdict; failing for it would train the reader to ignore a red result on the machines that have a real one.

### Host backend — [tools/host_runtime.py](backend/src/core/tools/host_runtime.py)

`HostRuntimePool` creates **one subprocess per session**, lazily. Same daemon, same protocol, no image. This is the default backend.

- `RLIMIT_AS` from `HOST_RUNTIME_MEM_LIMIT` on POSIX. **Windows has no equivalent without pywin32**, so there the cap is documented rather than enforced — do not claim otherwise in the UI. `LOCAL_RUNTIME_*` is still accepted as an alias for every `HOST_RUNTIME_*` field.
- Interrupt: `SIGINT` on POSIX, `CTRL_BREAK_EVENT` on Windows — which needs `CREATE_NEW_PROCESS_GROUP` at spawn, or the signal reaches the API process too.
- `get()` restarts a child that has exited (OOM kill, crash) rather than handing out a dead one.
- Runtime pip inside the daemon is off by default here: unlike a container, it would install into the environment the backend itself runs in. **On-demand installs happen in the parent** instead — [tools/packages.py](backend/src/core/tools/packages.py) runs `pip install --target <workspace>/.libs`, which the daemon has on `sys.path` and re-scans with `importlib.invalidate_caches()` before each execution. Two reasons: inside a sandboxed child the install sits behind a network policy that denies it, and Milestone 2's `library_install` gate that authorises it already runs in the parent, so the decision and the action were a process boundary apart. `SANDBOX_ALLOW_RUNTIME_PIP` is the master switch; when it is off the gate is not offered at all, because a question whose only possible answer is no is worse than no question.

### Sessions — [session.py](backend/src/core/session.py)

Every browser gets a `Session`: its own datasets, **reference documents**, catalog, chat history, workspace directory and container. Resolved from the `X-Session-Id` header (or `?session=`), TTL-reaped, and capacity-bounded. There is no global dataset state.

`DatasetHandle.table_key` is the sanitised name generated code addresses the table by (`Q3 sales (final).csv` → `tables['q3_sales_final']`). It also names the file under `workspace/tables/`.

### Export — [agent/export.py](backend/src/core/agent/export.py) + [routes/export.py](backend/src/api/routes/export.py)

Milestone 9. Turns a turn's *real executed steps* — pulled from the investigation, never reconstructed from the model's description of what it did — into a runnable script or notebook. The same grounding rule `check_grounding` applies to the answer text applies here to code: report what ran, don't launder it into something cleaner-looking.

- **Two callers, one builder.** The always-on per-turn artifact (`orchestrator._write_script`) and the on-demand `GET /api/export/{message_id}` route both go through `export.build_script`/`build_notebook`, so there is exactly one definition of "what actually ran" rather than two implementations that can drift.
- **Two loader shapes, because the two callers ship to different places.** The always-on artifact stays inside the session workspace, next to the per-table Feather files `Session._materialize` already wrote there, so it reads those in place (`tables/{key}.feather`). The on-demand export *leaves* the workspace — it is downloaded and may be opened on a machine with no Wizard session at all — so it ships its own CSV copies (`bundle_files`) and reads those instead (`data/{key}.csv`). `dataset_loader_lines` takes the file template and pandas reader as parameters for exactly this, rather than being two near-duplicate functions.
- **A connector-sourced table is never embedded, either way.** It is looked up by name at run time through `ConnectionStore.by_name`, the same object Milestone 4's write-back gate already uses — the exported script names the connection, never a credential, extending `ConnectionSpec`'s "reference, never the secret" rule to generated code instead of just to what's persisted to disk.
- **Chat messages now persist each turn's real executed steps** (`chat_messages.meta`, an additive `MIGRATIONS` column in `database.py`) specifically so a turn stays exportable after a *later* turn overwrites the workspace's single always-on `analysis.py`. The always-on artifact is one file per session; the persisted steps are one row per message, which is what makes "export the analysis from two questions ago" answerable at all.
- **A zip, not a bare file, whenever a file-based table needs to travel with the script** — a connector-sourced table never does, since it's re-fetched by name instead. The route decides this per export by checking whether `bundle_files` came back non-empty, not by asking the caller to know in advance.
- **User-initiated only**, from the results UI beside Copy and Save-as-skill — tying review, export and promote into one action row rather than export being buried in the generic workspace file list, per guiding principle 7.

### Reference documents — [ingest/documents.py](backend/src/core/ingest/documents.py)

Data dictionaries, metric definitions, business rules — `.md/.txt/.rst/.html` always, `.pdf/.docx` when `pypdf`/`python-docx` are installed (imported inside the function, so neither is required to start). Chunked on **paragraph** boundaries, not fixed width: a definition cut in half yields two chunks that each retrieve well and neither of which states the rule. Retrieval goes through `embedding_service`, so it degrades to lexical overlap with no model loaded.

`.txt` is deliberately claimed by both loaders — a tab-delimited export and a plain-text dictionary are both real. The endpoint decides which. Nothing structured (`.csv`, `.parquet`, `.xlsx`) may appear in the document list; a test pins that.

### Connectors — [core/connectors/](backend/src/core/connectors/)

Databases, document stores and object storage, as an ingest source **parallel to file upload**. The deliverable is the interface, not a list of supported engines: which databases an install can reach is a question of which driver is installed.

**Snapshot, not live pushdown.** A connection is read in the *parent* process and registered through `Session.add_dataset`, which materialises `workspace/tables/<table_key>.feather` for the daemon exactly as an upload does. Generated code never holds a connector and never opens a socket, so Milestone 3's `HOST_SANDBOX_NETWORK=deny` seal keeps meaning what it says — and the write gate is real rather than advisory, because a write is a parent-side action the agent has to ask for. `fetch(query)` exists on the protocol and is parent-side only. A regression test asserts no DSN or secret reaches the workspace.

Split like `security/sandbox/`, so most of it is testable with nothing running: `spec.py` (inert, JSON-safe — this layer's `policy.py`), `base.py` (the `Connector` Protocol), `registry.py` (this layer's `providers.py`), `store.py`, `gate.py`, `ingest.py`, and the three reference drivers. **SQLite is what makes it testable** — no third-party driver, so the suite exercises the real connector against a real database offline.

- **Registry keyed by an explicit `kind`, not by a URL scheme sniffed from the DSN.** The two look equivalent: `s3://` and `mongodb+srv://` are not SQLAlchemy dialects, an object store often has no scheme at all, and a string matching neither would resolve to whichever driver matched first instead of to an error. Adding a connector is a `register()` call and a module; a unit test registers one from outside the package, which is the extensibility claim asserted rather than described.
- Drivers are probed with `find_spec`, never imported — this renders on every page load. A missing driver is **listed with the pip command**, not hidden: hiding it lets a user conclude Wizard cannot reach Postgres when one install would.
- `relational.py` builds its bounded read as `select().limit()` off a reflected table rather than assembling SQL. The row limit is spelled differently per dialect (`LIMIT`/`TOP`/`FETCH FIRST`) and only the dialect knows which; reflection also means the identifier is never interpolated.
- **A connection is configuration; the tables it imports are data.** The non-secret half persists to `connections.json` in the platform config directory (Milestone 9's exported script looks a connection up *by name* at run time, which is impossible if it died with a TTL-reaped session); the secret half goes to `credential_store` under `connection:<id>`. `ConnectionSpec` carries a *reference* to a credential, never the credential — so there is no field to forget to strip, which is stronger than remembering to. Both files go through `utils/fileperms.restrict`, shared rather than copied because the Windows half's grant-to-the-token-SID rule was already got wrong once.
- `providers_with_keys()` filters out anything containing a colon. The keyspace is shared, so without it a saved database password is reported as a configured model provider.
- **The dataset name is built dot-free.** `DatasetHandle.table_key` derives from `Path(name).stem`, so a table named `public.orders` would have stem `public` — every table from one connection collapsing onto one key, one feather file and one `tables[...]` binding, each overwriting the last. `spec.dataset_name()` is the only thing that builds it, and a test pins two tables from one connection staying distinct.
- `CONNECTOR_MAX_ROWS` is not optional. An upload is capped by `MAX_UPLOAD_BYTES` before anything parses it; a table is not, and `_materialize` writes each frame up to three times. Truncation is reported through `profile.truncated`/`original_rows`, which already exist and are already rendered.
- `DatasetHandle.origin` names the connection a table came from, so one data-policy decision covers every table from a source **including ones imported later**. Passed to `should_redact` as its own argument rather than split back out of the name — a prefix test would give an uploaded `sales.csv` the policy set for a connection called `sales`.

**Consent — [connectors/gate.py](backend/src/core/connectors/gate.py).** `orchestrator._permit` needs a `RunState`, an emitter and a socket; a user clicking Import has none. Calling the broker anyway would not fail — it would emit to nobody, wait out `AGENT_CONSENT_TIMEOUT` and return a denial nobody declined. So the REST sibling adds one rule: **an authenticated request from the user is itself the answer to an `ask`**, and it records the grant so the agent inherits the answer. Asking someone to confirm the button they just pressed is theatre, and the fastest way to get a real prompt clicked through is to show three meaningless ones first. `deny` stays terminal — it is a real third state, not a stronger `ask`. What is gated is not *saving* a connection, which reaches nothing, but **opening** one.

Write-back has three independent locks: `spec.read_only` off for that connection with its name typed back (checked first, and **without asking anything** — a question whose only permitted answer is no is worse than no question), `db_write` not denied, and a grant recorded per `connection:table` so approving a write to `staging.results` is not approving one to `prod.orders`. `db_write` keeps `always_ask`, so no profile reaches it. Enabling write-back does **not** grant a write: every session still asks the first time.

`db_connect` and `db_write` flipped to `live=True` **in the same change that gave them call sites** — a category reported live with nothing behind it is the same untruth as a toolkit entry advertising an absent library. `tool_use` stays reserved.

### Skills — [core/skills/](backend/src/core/skills/)

Reusable know-how the agent can **cite**. Everything else it remembers is private and opaque — the semantic cache, working memory and trajectories all change what it does with nothing anyone can read, edit or name. A skill is a `SKILL.md` file: frontmatter plus instructions, in the convention this ecosystem already uses.

Split like `core/connectors/` so most of it runs with nothing started: `spec.py` (inert data), `frontmatter.py` (pure parser), `loader.py` (one file), `registry.py` (the layered store), `promotion.py` (the candidate pipeline).

**Three layers, ascending precedence: built-in → user-global → project.** A name defined twice resolves to the more specific layer, and the shadowed copy is still listed with `shadowed_by` set — without that, editing the built-in copy appears to do nothing and there is no way to find out why. Built-in lives in the checkout and is **read-only**: an edit would be lost on the next `git pull`, so `write`/`delete` raise `SkillNotWritable` and the API 409s with the reason rather than accepting a write it will discard. The documented override is a user skill of the same name.

**The user layer is `config_dir()/skills`, not `~/.wizard/skills`.** The spec writes the latter; Milestone 1 established `utils/appdirs.config_dir()` as the single answer for user-level state and Milestone 8's CLI manages that same directory. A Linux-shaped dotfile path on Windows is what guiding principle 5 forbids, so skills sit beside `credentials.json` and `connections.json`.

**A skill may never carry executable code.** This is Milestone 6's trust boundary, decided early because it shapes the format now, and it is enforced in `loader.load_skill` rather than documented: a skill directory containing `.py/.sh/.ps1/.bat/.exe/.so/...` is **refused, naming the file**. Refused rather than ignored — the author should find out at load, not discover later that half their skill never ran. Python inside a skill body is illustrative text; the only way anything derived from it executes is the worker writing code that then passes `CodeGuard.scan` and runs in the sandbox, exactly as for a question typed by hand. Deciding this now means the registry, the review UI and Milestone 6's pull flow never grow a second, weaker path.

`frontmatter.py` is a **restricted YAML subset**, not PyYAML — which is only a transitive dependency here, and which will happily return aliases and nesting nothing downstream expects from a file Milestone 6 fetched from a stranger's repository. A parser that can only produce strings and lists of strings cannot be talked into any of it. Anything outside the subset raises, naming the line.

**Retrieval: planning prompt + the `consult` action. Not the worker prompt.** The planning prompt is built once per turn; the worker prompt is rebuilt per iteration *and* per correction retry, so a block there is paid for N times. The decision and answer prompts already carry the plan, which is what the skill informed — letting the body ride along inside `state.plan` would be the `<think>`-tag defect in a different shape. `regression/test_turn_cost.py` pins all of it, including that the block never exceeds `SKILLS_MAX_CHARS`.

Retrieval costs **no LLM call**: it is a ranking over local files, so a compact turn is still three round-trips with skills installed.

**Without an encoder, ranking is question-coverage, not the hashing encoder's cosine.** Measured against the two shipped skills, the fallback encoder scored "what is the capital of France" at **0.368** and "which cohorts are driving churn" at **0.172** — it ranked the irrelevant query higher, so no floor could separate them. A six-word question against a 1,200-character passage is dominated by sketch collisions. Coverage — what fraction of the question's content words the passage contains — gives 0.0 and 0.667 for the same pair, and lands on the same scale as a transformer's cosine, which is why one `SKILLS_MIN_SIMILARITY` serves both. Normalised by the *query*, not the union, unlike `retriever.lexical_overlap`: dividing by the union punishes a passage for being long, and scored a perfect topical match at 0.04.

`consult` is now offered when the session has documents **or** skills are installed. It was gated on documents alone, which made the installed skills unreachable through the action the milestone says they should be consulted by — and no documents is the ordinary state of a fresh install.

**Promotion — [skills/promotion.py](backend/src/core/skills/promotion.py).** The spec says a *trajectory* that succeeds repeatedly gets offered; the `trajectories` table does not hold that. `save_trajectory` fires only when `retry_count > 0`, so it records failure-then-fix and nothing else, and waiting for a repeat of one would mean the offer effectively never appears. Two kinds are counted, **separately**: `recurring` (a successful turn whose question recurs — the signal that actually means "you keep doing this") and `recovery` (a failure-then-fix that has recurred — a trap rather than a routine, and a different skill to write). Threshold is 3.

**A turn answered from the semantic cache is counted**, which is the opposite of the obvious rule and the only way this works: the cache short-circuits the same question against the same schema, so the second and third times somebody asks are exactly when nothing is re-derived. Skipping them left the counter permanently at one. A cache hit is the system recognising the question as one it has already answered — the strongest evidence of recurrence there is. What a cached turn must *not* do is overwrite the stored draft, so its `plan` (a "reused a verified solution" placeholder) is passed empty and `bump_skill_candidate` keeps the real one.

The offer is emitted **exactly once**, at the threshold — re-offering every turn is how a useful prompt becomes one people click away. A dismissed or promoted candidate still participates in matching, or the next occurrence would insert a fresh row and bring back the offer just declined. `pending()` still lists it, so a card missed in the chat is findable on `/skills`.

`skill_candidates` deliberately has **no `session_id`** — "you keep doing this" is a claim about many sessions — so `delete_session_data` does not touch it and the test teardown has to clear it explicitly.

Nothing here writes a skill. Crossing the threshold produces a frame; a file appears only when the user confirms. **A draft is never asked of a model** — the grounding layer's rule applied to promotion. Where its content comes from depends on what was recorded: a candidate-backed draft is built from the plan and code that actually ran, and a draft for a question with no candidate row is built from the question alone, since there is nothing else true to say about it.

**Two routes into promotion, one form.** The threshold offer is the agent's; `POST /api/skills/draft` is the user's — "save *this* one" about an answer already on screen, with no threshold, which the milestone lists as its own bullet. It needs no new bookkeeping because every successful turn already records a candidate, so `promotion.find` resolves the question to the row holding the plan and code that ran. A question with nothing recorded still drafts, from itself: whether the button works must not depend on state the user cannot see. `candidate_id` comes back null there, and `POST /api/skills` settles a candidate only when given one.

**`skill_usage` answers "which analyses used which skill".** The `skill` frame is live and gone by the time anyone opens `/skills`, so the milestone's browser bullet cannot be served from it. One row per skill per turn — not per retrieval, since a skill can match at planning and again through `consult`, and the claim is "it informed that analysis". Recorded **outside** the success branch: a skill informed the plan whether or not the code that followed worked, and counting only the wins would hide the skill that is reached for and keeps failing, which is the one worth finding. Like `skill_candidates` it has no `session_id` — a skill's history must not reset because a browser tab was reaped — which is why the test teardown clears it too.

**Reading and writing a local skill is not a permission category.** Every layer is a local file the user put there, and promotion is a REST action they initiated — the same reasoning that leaves *saving* a connection ungated. **Installing one from GitHub is**, under `network` — see below.

### Installing a skill from GitHub — [skills/install.py](backend/src/core/skills/install.py)

Skills become shareable the way ClawHub's are: pulled from a repository or gist, not hosted by Wizard. Four modules, split like the rest of the package so most of it runs with nothing started: `source.py` (inert — URL parsing and the install record), `fetch.py` (the only thing that dials out), `index.py` (the local provenance file), `install.py` (the flow).

The order is the security argument: **parse → resolve to a commit → stage → the user reads it → approve.** Nothing reaches the agent between the third step and the fifth.

- **The Contents API, not a tarball.** `GET /repos/{o}/{r}/contents/{path}?ref=<sha>` returns a JSON directory listing or one file's base64 content. An archive would be fewer requests and is the wrong choice: the **executable-payload refusal is enforced from the listing, before a byte of content is fetched**, nothing arbitrary is ever written to disk (only the `SKILL.md` text — so no zip-slip, no traversal, no symlinks, no decompression bomb), and a repository pointed at by mistake is refused by count rather than discovered one request at a time. What it costs is one request per file and 60/hour unauthenticated, reported with GitHub's own reset time rather than as a generic failure.
- The rule itself is `loader.offending_names`, extracted from `executable_payload` so the on-disk check and the remote check apply the *same* function. Two implementations of a security boundary is two chances for them to stop agreeing.
- **Pinning is its own step.** The ref resolves to a SHA once and every later request carries it, so two requests against a moving branch cannot straddle a push and assemble a "pinned" install from two commits. A gist pins to `history[0].version`.
- **Provenance is what we wrote, never what was fetched.** M5's loader read `source_url` and `pinned_sha` out of the file's own frontmatter — harmless when the only writer was this machine, and a **spoofable claim** once the file comes from a stranger: a hostile `SKILL.md` could name any commit in any repository and the UI would render an unearned badge. The loader now ignores both keys and `install_index.overlay` stamps them on after the scan, from `config_dir()/skills/installed.json`. Pinned in `regression/test_regressions.py`. This is also why the index is a sidecar rather than extra frontmatter — it keeps the installed file byte-identical to upstream, which is what makes the update diff exact.
- **Staged into `config_dir()/skills-pending/`** — a *sibling* of the user skills root, not a hidden directory inside it. The registry never scans it, and a sibling cannot become live through one bug in `skill_paths`' `iterdir`. It is also visible in a file browser, which "pending" should be. Staging goes to disk, so a review interrupted by a closed tab does not cost a second fetch.
- **Pin, don't track.** `check_update` re-resolves the **stored** ref — never a newly-chosen branch — and compares SHAs. Same: nothing is written and it says so, without even a content fetch. Different: a `difflib.unified_diff` comes back and *still* nothing is written; applying takes a second call. The diff is against **the file on disk**, not against upstream at install time — those differ the moment somebody edits an installed skill, and diffing the wrong one presents their own edits as incoming changes.
- **Gated by `network`, not refused by `local-only`.** No session data, schema or rows leave — this is a download of instruction text, the same shape as `POST /api/models/download`, which the mode does not block either. `OUTBOUND_TOOLS` is scoped to tools the *agent* invokes mid-analysis, where the query itself is derived from the user's data. The gate goes through `permissions.authorize`, **lifted out of `connectors/gate.py`** when this became its second caller; `connectors/gate.py` keeps `require_writable` and re-exports the rest, so no M4 call site moved.
- **Install is user-initiated only — never an agent action.** A fetched skill is untrusted text that goes into the manager's prompt; if the manager could also install skills, a fetched skill could instruct the agent to fetch more. That is wormable, and a consent prompt does not close it because the prompt's wording would be written by the thing under review.
- The optional token lives in `credential_store` under **`registry:github`** — namespaced with a colon on purpose, because `providers_with_keys()` filters on one and a bare `github` key would be reported on the models page as a configured model provider.
- `SKILLS_REGISTRY_API` exists so GitHub Enterprise is a setting rather than a fork; `install._enterprise_hosts` derives the web hostname from it rather than making the operator configure the same thing twice. `conftest.py` pins it to `http://127.0.0.1:1`, so a test that forgot to inject the fake fetcher fails instantly instead of reaching github.com.
- **The CLI ships now**: `python backend/main.py skills add|list|update|discard|remove|token`, over the same functions the routes use. An optional positional and a subparser cannot coexist in one argparse parser, so the first word selects between two parsers — `python backend/main.py data.csv` is unchanged. Milestone 8's binary will front these rather than reimplement the fetch. `discard` is separate from `remove` because they act on different things: `remove` uninstalls through the registry, and a *staged* skill is not installed, so without its own verb the CLI could stage a skill it had no way to clear.
- **A gist request carries the revision in its path** (`gists/{id}/{sha}`). `gists/{id}` answers with the current revision, so reading through it after pinning to `history[0].version` would record a commit that does not identify the bytes read — and `check_update` would compare a stored SHA against a body fetched from HEAD. The pin held for repositories and silently did not for gists.

### Context budgeting — [retriever.py](backend/src/core/rag/retriever.py) + [prompts.py](backend/src/core/prompts.py)

`generate_system_context` does not dump the whole frame. Columns are selected by relevance to the question (columns named in the question are always kept), and memories, trajectories and few-shot examples are retrieved semantically. Everything degrades to lexical scoring when no embedding model is loaded.

"Named in the question" goes through `mentions_column`, which matches on **word boundaries**. A substring test looks equivalent and is not: a column called `C` matches inside "check" and `id` matches inside "provide", so nearly every column reported as explicitly requested and the budget stopped budgeting.

`prompts.TOOLKIT` is a **catalogue, not a promise**. `_toolkit_block(session_id)` filters it through `runtime.capabilities(session_id)`, which asks the runtime what it can actually import. That removes a coupling which had to be maintained by hand and was wrong in both directions: scikit-learn and statsmodels sat installed and unadvertised for months, so generated code hand-rolled statistics that were already there, and duckdb was advertised to a process without it, costing a correction retry every time.

Entries are **atomic** — one naming three libraries is dropped unless all three are present — which is why charts and file output are listed separately rather than together.

`_visualization_rules` and `_workspace_root` are capability- and backend-aware for the same reason: `PLOT_FORMAT=html` needs plotly, which the `core` image tier does not ship, and the writable root differs per backend.

`runtime.TIER_MODULES` mirrors the Dockerfile for the case where no runtime exists yet. A test parses the Dockerfile and asserts the two agree.

### Infrastructure — [infra/](backend/src/core/infra/)

`get_cache()` and `get_queue()` return in-process implementations by default. Setting `REDIS_URL` (with `redis` installed) swaps in Redis; if Redis is configured but unreachable, the cache degrades rather than failing the app. Nothing requires Redis.

### Persistence — [database.py](backend/src/core/database.py)

One SQLite file, `backend/data/wizard.db`, accessed through `db_mgr`. **Connections are pooled per thread and closed explicitly**, with WAL and a busy timeout — FastAPI dispatches blocking work through `asyncio.to_thread`, so several threads hit this database concurrently.

Tables: `semantic_cache`, `trajectories` (failure→fix pairs), `feedbacks`, `working_memory`, `chat_messages`, `schema_registry`. Additive column migrations run on boot from the `MIGRATIONS` tuple.

### Models — [llm/](backend/src/core/llm/)

`llm_provider` builds and caches clients keyed by (provider, endpoint, model, temperature, max_tokens, num_ctx), so per-session model selection is cheap. Every entry point has a streaming twin (`astream`, `stream_to`), and every one takes `max_tokens`.

**Pass the output budget for what the call is *for*.** `settings.output_budget("decision"|"plan"|"code"|"answer"|"review")`, clamped to `MAX_TOKENS` — clamped, not maxed, because `MAX_TOKENS` is what someone lowers when their context is small. `MAX_TOKENS` used to be the only number, so every call was allowed 4096 tokens of output regardless of purpose. That is free when a model stops on its own and ruinous when it does not: a decision worth sixty tokens could run to four thousand, which on a CPU-bound 1.5B model is four minutes. `max_tokens` is part of the client cache key, so two budgets do not share a client; `num_ctx` is *not* varied per call, because it is a load-time parameter and changing it would make the provider reload the model.

The Ollama client also sends `keep_alive` (the manager and worker alternate every iteration, so an eviction between them costs a reload from disk) and a request timeout via `client_kwargs` — `ChatOllama` has no `timeout` field, and without this a wedged daemon hung the turn forever while the OpenAI-compatible path had had a timeout all along.

### Fitting the models in memory — [llm/resources.py](backend/src/core/llm/resources.py)

The manager and worker alternate several times per question, so whether they can be **resident at once** decides whether that alternation is free or ruinous — and nothing in the app knew. Two 1.5B models coexist anywhere; two 7B models want ~14 GB, and on a 16 GB laptop also running a browser, this backend and a sandbox subprocess, the OS starts paging a model between tokens. That is not "slower" in the way a small model is slower; it is one to two orders of magnitude worse and it takes the desktop with it.

- `estimate_footprint` is **calibrated against a measurement**, not a datasheet: `qwen2.5:3b` is 1.93 GB on disk and reported **2.91 GB resident** at 8192 context, giving ~40 MB of KV cache and buffers per billion parameters per 1024 tokens. The estimator predicts 2.9 GB for it. It is deliberately biased high — over-estimating costs one reload, under-estimating costs a swap storm.
- `plan_resident_set` compares the pair against `MODEL_MEMORY_FRACTION` (0.60) of system RAM. Fits → both keep `LLM_KEEP_ALIVE` (30m), since an eviction between alternating roles costs a disk reload every iteration. Does not fit → `LLM_KEEP_ALIVE_SWAP` (30s), short enough to expire *while the other model is working*, so its memory is released before the other needs it. **Swapping on purpose is bounded; thrashing is not.**
- The only levers that reach the server per request are `num_ctx` and `keep_alive`. `OLLAMA_MAX_LOADED_MODELS` would be the direct control but belongs to the server process.
- `ModelSpec.keep_alive` is part of the **client cache key** — a client built when the models fitted must not be reused once they do not.
- Only `LOCAL_PROVIDERS` are planned. A gateway model occupies someone else's RAM, so budgeting this laptop around it answers the wrong question. Same model in both roles collapses to one footprint and never swaps.

### Reasoning models — [llm/reasoning.py](backend/src/core/llm/reasoning.py)

A model's private thinking is not its answer. `split_reasoning` / `strip_reasoning` remove `<think>`, `<thinking>`, `<thought>`, `<reasoning>` and `<reflection>` blocks; `ReasoningStream` does the same **incrementally** so token streaming survives, holding back only a trailing partial tag rather than the whole buffer.

The orchestrator knew about `<thought>` — the tag its own planning prompt asks for — and nothing else. With `deepseek-r1:1.5b` in the manager role that meant the raw chain of thought became `state.plan`, and **the plan is embedded in every later decision prompt and in the answer prompt**. One unrecognised tag pair did not cost one bad plan; it prepended a thousand tokens of deliberation to five later prompts on the machine least able to re-read them. It also broke action parsing (the discarded reasoning names every option while weighing it) and streamed the deliberation to the user as the answer.

`_extract_code` strips reasoning **first**, and that ordering is load-bearing: a model thinking out loud drafts code inside `<think>`, discards it, and writes the real thing afterwards — while `_extract_code` takes the *first* fenced block it finds. Searching the raw response runs the draft the model already rejected.

An unclosed block yields empty visible text rather than half a monologue; callers treat that as "nothing usable came back".

**The provider is per-request, not process-wide.** `settings.API_PROVIDER` is only the default. `ModelPreferences` stores a provider per *role*, so one run can plan on Ollama and generate code on LM Studio; `ModelSpec` therefore carries the resolved `base_url`, and that URL is part of the cache key — without it the same model name on two backends collides. Never read a provider URL directly from `settings`; go through `settings.provider_root_url` / `provider_openai_base_url` / `provider_api_key`, keyed by the provider actually in play.

`model_registry` enumerates what is really installed, per provider and cached per provider:
- **Ollama** → `/api/tags`
- **LM Studio** → `/api/v0/models` (native: real `type`, quantization, context length, load state), falling back to `/v1/models`
- **Anthropic** → `/v1/models` with `x-api-key` + `anthropic-version`. Asked only when a key exists: without one the endpoint returns 401, which would be recorded as an unreachable host and point the user at the wrong problem.
- **OpenAI and gateways** → `/v1/models` with a bearer token

Empty results are cached too, for a shorter TTL — a refused connect costs seconds, and one page load asks for both the list and a suggestion. `available_providers()` must stay network-free; it renders on every page load.

### What a provider *is* — [providers.py](backend/src/providers.py)

One row per backend: id, label, `kind` (local/cloud), `api_style` (ollama/openai/anthropic), default URL, which settings fields hold its URL and key, and whether it needs one. Adding Groq or a self-hosted vLLM is a row, not a code change.

This replaced an `if name == "ollama" … elif` chain repeated in four places — root URL, OpenAI base URL, API key, is-configured — plus a fifth copy of the local/cloud split in `llm/resources.py`. Two of the five were missed whenever a provider was added, which is how a gateway model ended up being sent an Ollama keep-alive.

It sits **beside `config.py`, not under `core/llm/`**, for the same reason `utils/hostinfo.py` does: `Settings` is built at import time and reads this table for its defaults, while `core.llm.__init__` imports `settings` back.

`is_cloud()` treats an **unknown** provider as cloud. That is the safe direction — it feeds the data-mode check, where calling something unrecognised "local" would open the hole the check exists to close.

`openai_suffix` is only non-empty for LM Studio, whose root is genuinely bare because `/api/v0` hangs off it. A hosted endpoint is configured with its version segment already in it, and appending one would break every existing install.

### Data mode — [core/data_mode.py](backend/src/core/data_mode.py)

**This is the mechanism behind the local-first promise.** Before it, "your data stays local" was a property of how somebody happened to configure their `.env`: a cloud provider assigned to a role was simply used, and the prompt — sample rows and all — went to it.

`local-only` / `cloud-only` / `hybrid`, session-wide, seeded from `settings.data_mode`. `local-only` **refuses** a cloud provider rather than skipping it; a hard boundary, not a preference.

**Enforcement lives in `LLMProvider.resolve`** — the one function all nine LLM call sites already pass through, so a session that pins its own `manager_provider` cannot route around it. A violation raises `DataModeViolation`, a subclass of `LLMUnavailableError` so existing handlers surface it, distinguishable because it is a policy decision and not a fault: the orchestrator catches it separately and does *not* append "check that the provider is running".

Three axes, deliberately separate:
- **mode** — which providers a role may resolve to.
- **policy** (`DataPolicy`) — how much of the data a cloud-bound prompt carries.
- **tools** — `web_search` is *unavailable* under `local-only`, not merely unchosen. The plan's `SEARCH:` directive is dropped with a warning rather than raising an approval prompt, because there is no consent that would make it allowed. `disabled_tools()` feeds the UI so this is stated up front rather than discovered mid-run.

Switching mode **clears any role assignment the new mode forbids**. Leaving it would mean the next question failed instead of running, and the setting the user chose to be safer would read as having broken the app.

Embeddings are a role like any other — text sent to be embedded is data — but a forbidden encoder **degrades to the hashing fallback** instead of raising. Retrieval getting worse is survivable; a failed question is not.

### Permission profile — [core/permissions.py](backend/src/core/permissions.py) + [agent/consent.py](backend/src/core/agent/consent.py)

**Two independent dials.** Depth (`fast`/`auto`/`deep`) is how hard the agent works on a question; the profile (`auto-approve`/`ask-always`/`custom`) is how often it stops to ask along the way. The same analysis run `deep`+`auto-approve` and `fast`+`ask-always` reaches the same quality of answer and differs only in consent prompts.

**Data mode outranks the profile, always.** The mode decides what is possible at all; the profile decides what is asked about among what already is. `local-only` still refuses web search outright — no profile can consent past it, and `_orient` checks `tool_allowed` *before* it consults the profile.

Consent used to be three unrelated special cases (a process-wide plan gate, a hardcoded search prompt, a path check that could only say no) plus one thing that asked nothing at all: **library installation was invisible**, happening inside the daemon on `ModuleNotFoundError` *after* the program had started. `CATEGORIES` replaces all of that with one vocabulary, so a new gated action is a row plus a call site.

| category | live | trigger |
|---|---|---|
| `library_install` | yes | `imported_modules(code) - runtime.missing_modules(...)`, checked **before** execution |
| `network` | yes | the plan's `SEARCH:` directive; installing a skill from GitHub |
| `workspace_write` | yes | a literal path the guard rejected, defaulting to `deny` — exactly what the guard already did |
| `db_connect` | yes | opening a saved connection — `/schema`, `/import`, `/test`. Saving one is not gated; it reaches nothing |
| `db_write` | yes | writing a session table back to a source, `always_ask`, subject `connection:table` |
| `tool_use` | **no** | declared for a later milestone; the UI says nothing reaches it yet |

- `db_write` carries `always_ask=True`: it never resolves to `allow` from a profile, and `set_ruling` **raises** rather than silently clamping, so the API can 400 with the reason. Write-back is enabled per connection, once, deliberately.
- **Default is `ask-always`, not `auto-approve`.** Under it every category is at least as consultative as it was before the profile existed. Defaulting to `auto-approve` would have made an upgrade silently stop asking about web search — a trust regression shipped by a milestone about trust.
- **The guard is not weakened.** A `workspace_write` grant records the directory in `PermissionState.extra_roots`, which `CodeExecutor.guard` unions into the guard's roots; the code is then re-scanned normally. `GuardVerdict.only_paths` is what makes this safe — a program mixing a path violation with a banned import is never offered for consent, or one prompt would wave through the other.
- Grants are session-scoped and **not persisted**: consent for this analysis is not consent forever, and a grant outliving its session is a permission the user can no longer see to revoke. Tightening the profile clears them.

**`AGENT_REQUIRE_APPROVAL` is untouched and stays separate.** That gate is about the *plan*, fires in `_orient`, and is turn-terminating: the run returns `awaiting_approval` and the client re-sends a new turn carrying the approved plan. That shape is right there — nothing has been spent yet.

It is wrong for an action chosen at iteration four, which is why `consent.py` exists. `ConsentBroker.ask` parks the turn on a future keyed by session id (like `usage_ledger`, so `core/agent` holds no transport state) and the run keeps its investigation. **Anything that can suspend can hang**, so a timeout, a cancel, a disconnect and `abandon()` all resolve identically — *denied, with a reason*. `orchestrator.run(can_prompt=...)` is how a transport declares it has a reply channel; REST and the CLI pass `False`, and there an `ask` becomes a stated denial rather than a request nobody will see.

A denial does **not** end the turn. `_act_code` records a failed `Step` and returns, so the loop routes around it exactly as it does any other failed sub-task — declining once must not cost the whole question.

**`chat.py`'s receive loop no longer awaits the run.** It used to (`ensure_future` then immediately `await`), so no frame sent during a turn was read until the turn finished — which is also why `cancel` could not interrupt anything. Error handling moved inside `run_turn` for this. An `approval` frame with an `id` is routed to the broker *before* the "a run is already in progress" check, since that check exists to reject a new turn, not an answer to the running one.

### Redaction — `should_redact` + [prompts.py](backend/src/core/prompts.py)

`generate_system_context(..., redact=True)` keeps shape, column names, dtypes, null rates and semantic types, and drops every real value: the `example` column, the `head(3)` glimpse, the `describe()` summary, and categorical distinct values (replaced by a count). It adds a line telling the model values were withheld and must be computed — without it, an empty glimpse reads as an empty table.

**Decided per prompt, from the provider that prompt is going to**, not once per turn. Under `hybrid` with a cloud manager and a local worker, the planner is redacted and the code generator is not.

**Execution output is deliberately *not* redacted.** The answer is synthesised from real stdout by `create_answer_prompt`; withholding it would leave the answering model nothing to answer from. The UI says this precisely rather than implying more than is true.

Settable **per source** as well as per session (`DataPolicy.per_dataset`), because a published reference table and a payroll export do not deserve the same answer. "Follow default" is a real third state — an explicit override does not track the session setting. An override is dropped with its dataset, so re-uploading a file of the same name cannot inherit a policy set for a different one. Milestone 4's connections are sources too and reuse this field.

### Credentials — [core/credentials.py](backend/src/core/credentials.py)

Keys live in `credentials.json` under the platform config directory ([utils/appdirs.py](backend/src/utils/appdirs.py) — `%APPDATA%\Wizard`, `~/Library/Application Support/Wizard`, `$XDG_CONFIG_HOME/wizard`), which Milestone 8's CLI will manage too. `WIZARD_CONFIG_DIR` overrides it; the test suite pins it so no test touches a developer's real file.

**This is not encryption at rest** — the guarantee is the OS's access control, the same one `~/.aws/credentials` has, and it is stated plainly rather than dressed up. Encrypting would need a passphrase at every backend start (breaking the unattended `wizard start` of Milestone 8) or a key stored beside the ciphertext, which protects nothing. **OS keychain integration is deliberately not taken**: three platform backends plus a dependency, and Secret Service is often absent on headless Linux, so a file fallback would be needed anyway. Everything goes through `credential_store`, so a keychain backend can be added later without touching a caller.

Permissions are **enforced on all three platforms**, not documented on two. POSIX gets `0600`. Windows gets inheritance stripped and a single-user ACL via `icacls` — granted to the **SID read from the process token** (`whoami /user`), never to `%USERNAME%`: that is an ordinary environment variable and on one dev machine read `Wizard`, so the first write succeeded, the ACL handed the file to a user that did not exist, and the second write failed with `PermissionError`. The result is verified afterwards and rolled back to inherited permissions if the file came back unwritable, because a credentials file nobody can write is worse than one with default permissions.

Resolution order is **environment/settings first, then the store**, so a container or CI run behaves exactly as configured. Keys are never logged and **never returned by any route** — only `has_key` and a masked `…abcd` hint.

### Cost — [llm/usage.py](backend/src/core/llm/usage.py)

`extract_usage` reads `usage_metadata`, then `response_metadata` (`token_usage`, or Ollama's `prompt_eval_count`/`eval_count`), then falls back to a `len/4` estimate flagged `exact=False`. Three shapes on purpose: the reported field differs between langchain-ollama, -openai and -anthropic and between versions of each, and a meter that silently reads zero is worse than one that admits it approximated.

`usage_ledger` is keyed by session id rather than held on the `Session`, so `core/llm` does not import `core.session`. A streamed call is booked **once**, in `astream`, from whichever chunk carried the metadata — booking per chunk would multiply every plan and answer by its token count.

**An unpriced model reports tokens and `cost_usd: None`, never a guess**, and is named in `unpriced_models` so the readout can say the total is a floor. That is the grounding layer's "report, don't invent" applied to money. Under `local-only` the API returns `local_only: true` and no cost at all — `$0.00` reads as a computed figure, and the true statement is that there is no meter.

### Installing models — [llm/downloader.py](backend/src/core/llm/downloader.py)

Getting a model was the one setup step that sent you out of a local-first tool and into a terminal or the LM Studio window — and it is the step a first-time user hits first, with an empty picker and nothing to select. `POST /api/models/download` fixes that; the providers disagree about how, so the disagreement lives in this one module.

- **Ollama** has a real API: `POST /api/pull` streams NDJSON with byte counts, `DELETE /api/delete` removes. Nothing else needed.
- **LM Studio** has no download API — `/api/v0` is read-only. The only scriptable route is the `lms` CLI that ships with the app, so it is spawned. It reports a percentage rather than bytes and **has no delete verb at all**; both are reported as limits rather than faked, which is what `capability()` is for.
- **Gateways** host their models. Asking says so instead of offering a button that fails.

The model name reaches an argv, so it is **validated, not escaped** (`is_valid_model_name`). A URL is matched against the Hugging Face pattern *first* — testing the general pattern first looks equivalent and is not, because `:` and `/` are both legal in a bare model name, so `https://evil.example.com/a/b` matches it and the host restriction never gets a say. Requiring an alphanumeric first character is the whole flag-injection story: `--help` cannot get through, so no separate guard is needed.

Downloads run on a thread and are **polled, not streamed** — a pull runs for minutes and survives a reload. `DownloadState.finish()` writes `finished_at` *before* `status`, because the sweep reads a missing timestamp as "finished long ago": the other order lets a failed download become terminal and instantly sweepable, so the row vanishes before the UI shows the error.

`lms_executable()` checks PATH then `~/.lmstudio/bin/` — the installer does not add itself to PATH on Windows. Inside a container it finds nothing even when the LM Studio *server* is reachable over the network, and `capability()` says that rather than letting the button fail.

### Config — [config.py](backend/src/config.py)

Pydantic-settings singleton reading `backend/.env` (see `backend/.env.example`). Notes:

- `API_PROVIDER` is the *default* provider, not a global switch — see the models section. `MODEL_TYPE` exists only so older `.env` files still validate. Neither is a `Literal` any more: the set of backends is data in `src/providers.py`, and a field validator rejects an unknown name at boot, which is the same loudness a `Literal` gave without a third place to edit.
- **`DATA_MODE` empty means "derive it"**: `local-only` on a fresh install, `cloud-only` when `API_PROVIDER` is already a cloud backend. Derived rather than defaulted so upgrading a working cloud install does not break it in the name of protecting it. `DATA_SCHEMA_ONLY` defaults **on** — the conservative option is the one that should need no decision.
- `openai` used to mean "whatever `GATEWAY_API_URL` says". It now means `api.openai.com`, so an install that had pointed it at its own gateway has that URL carried across — but only when `API_PROVIDER` is `openai`, or a `custom_gateway` URL would hijack the real one.
- `LMSTUDIO_BASE_URL` is stored as a bare root; a pasted `/v1` suffix is stripped by a validator, because discovery needs `/api/v0` off the same root. LM Studio binds loopback only until "Serve on Local Network" is enabled, which is the usual cause of an empty picker from inside Docker.
- `LLM_NUM_CTX` reaches Ollama only. OpenAI-compatible servers fix context length when the model is loaded, so it is deliberately not sent there. It is **derived from the host** (laptop 8192 / server 16384 / hpc 32768) and `0` means "derive it". This is a *load-time* parameter: it fixes the KV cache reserved for every resident model, so asking for 16k when prompts reach 2k evicts the worker to make room for the manager on every iteration. Prompts here are budgeted and do not reach 8k — the measured compact-tier turn is ~2,100 prompt tokens across all three calls.
- **Every local provider's base URL is rewritten from `host.docker.internal` to `127.0.0.1` when the backend is not containerised** (the loop is driven by the descriptor table's `url_field`, so a new local backend is covered without touching the validator), and only when the user did not set them. That hostname is how a container reaches its host and is correct inside compose (which passes it itself); outside one it is a name Docker Desktop happens to add to the hosts file, so it resolves on a dev machine that has Docker and fails outright on one that does not — exactly the Docker-less install the local execution backend exists to serve. The shipped `.env.example` therefore leaves both commented out.
- The shipped `.env.example` also leaves `LLM_NUM_THREAD` and `LLM_NUM_CTX` unset. A value in the example file is copied to every machine and defeats the derivation on all of them, which is how a 4-physical-core laptop ended up running eight inference threads.
- `cors_origins` / `cors_allow_credentials` are resolved together: a wildcard origin forces credentials off, because the combination is invalid in every browser.
- `PLOT_FORMAT` is coupled across two places — the visualization rule in `create_prompt` and the artifact branch in `_execute`. Change both.
- `SANDBOX_ENABLED=false` disables container creation entirely; `EMBEDDINGS_FORCE_FALLBACK=true` skips every real encoder. Both are set in CI, alongside `EXECUTION_BACKEND=inprocess`.
- **`SYSTEM_PROFILE` finally does something.** On `auto` the host is measured at boot ([utils/hostinfo.py](backend/src/utils/hostinfo.py)) and `LLM_NUM_THREAD`, `QUEUE_MAX_WORKERS`, `SANDBOX_MEM_LIMIT`, `HOST_RUNTIME_MEM_LIMIT` and `SESSION_MAX_ACTIVE` are derived from it. It was previously read by nothing at all, so those were server constants on every machine: eight inference threads on a four-core laptop, and `32 x 2g` = 64 GB of permitted containers.
- The derivation runs in a `model_validator(mode="after")` and **only fills fields the user did not set** — `model_fields_set` carries anything from the environment or the .env. A **blank** string counts as unset, because `docker-compose.yml` passes optional knobs as `${VAR:-}` and an empty environment variable is still present.
- `hostinfo` is under `utils/`, not `core/infra/`: `Settings` is constructed at import time and `core.infra.__init__` imports the cache, which imports `settings` back.
- Host sizing and `AGENT_TIER` are **separate axes**. How much memory a runtime gets depends on the machine; how many iterations the agent gets depends on the model. A frontier gateway model still gets a `full` budget on a laptop.
- `HOST_SANDBOX` (`off`/`best-effort`/`require`) and `HOST_SANDBOX_NETWORK` size the OS sandbox — see that section. `EXECUTION_BACKEND` (`host`/`docker`/`inprocess`) picks the runtime; `SANDBOX_TIER` (`core`/`standard`/`full`) picks how much toolkit the image installs. `settings.docker_backend_allowed` / `host_backend_allowed` express *permission* only — whether Docker is reachable belongs to `core.tools.runtime`, since config cannot import the sandbox. `host_backend_allowed` is true under `docker` too, because that is where an unreachable daemon lands.
- **`MODEL_NAME` / `WORKER_MODEL_NAME` / `VISION_MODEL_NAME` are empty by default**, meaning "use whatever this provider has installed", resolved through `model_registry.suggest`. Setting one pins it. They used to be hardcoded Ollama tags, which made those two models load-bearing — both 404 on LM Studio and on every gateway.
- `AGENT_TIER` (`auto`/`compact`/`balanced`/`full`) sizes the loop. On `auto` it is inferred from the manager model's parameter count via `tier_for_parameter_size`: <4B compact, 4–30B balanced, ≥30B full. Gateways report no size and get `balanced` — guessing `compact` would cripple the strongest models available.
- `settings.budget_for(mode, parameter_size)` is the single place mode and tier combine. `TierBudget` carries its own `tier` name so callers never reverse-match numbers back to a tier. `fast` returns `allow_verification=False` — verification is a second code generation *and* a second execution. `deep` returns `allow_decisions=True` on every tier.
- `TierBudget.max_columns` reaches the **worker and planner** prompts, not only `inspect`. It existed and was ignored there, so a compact model sized for 25 columns was handed schema, statistics, sample rows and categorical values for 60.
- `AGENT_MAX_ITERATIONS` is a hard ceiling above the tier, deliberately not derived: a runaway loop on a paid gateway is a billing incident.
- `SKILLS_BUILTIN_DIR` and `SKILLS_PROJECT_DIR` are **empty by default, meaning "derive it"** — the checkout's `backend/skills/` and `.wizard/skills` under the working directory. The user root is never configurable: it is always `config_dir()/skills`, so one machine's layout cannot be copied onto every machine through `.env.example`. The test suite pins the two derivable ones, which is what keeps the shipped skills out of the suite.
- `SKILLS_REGISTRY_API` bounds nothing on its own; it is what makes GitHub Enterprise a setting rather than a fork, and is left commented in `.env.example` because `api.github.com` is right for everyone else. `SKILLS_FETCH_TIMEOUT` / `_MAX_BYTES` / `_MAX_FILES` bound the one thing in this codebase that waits on a host nobody in the project controls.

### Embeddings — [embeddings.py](backend/src/core/embeddings.py)

Resolution order: **provider endpoint → local sentence-transformers (only if installed) → hashing encoder**. Nothing here raises; a retrieval feature degrading always beats a question failing.

`sentence-transformers` is no longer a dependency. It requires torch, and on linux/x86_64 torch hard-depends on eleven `nvidia-*-cu12` wheels — ~2.8 GB of compressed wheels installed with or without a GPU, to run a 90 MB MiniLM model. It was the single largest thing in the backend image, larger than the whole analysis sandbox. See `requirements-optional.txt` for how to put it back **from the CPU index**.

- Ollama: `POST /api/embed` → `{"embeddings": [[...]]}`. Everything else: `POST /v1/embeddings` → `{"data": [{"index", "embedding"}]}`, **sorted by index** — order is not promised, and a swap attaches every vector to the wrong text without anything downstream noticing.
- The model is discovered through `model_registry`'s own classification, then **probed once** before adoption: a name that classifies as an embedding model is not the same as one the server will embed with.
- **The encoder is resolved at startup on a background thread** (`embedding_service.warm()`), and `encode` answers from the hashing fallback while that is in flight rather than blocking. Resolving it lazily put a cold model load on the critical path of the first question: measured at **51s** — a 20s timeout, then a 90 MB download, then a failure — with the model server completely idle throughout. `EMBEDDING_COLD_TIMEOUT` (180s) applies to the adoption probe only, because loading a model off disk is not the same operation as using one; `EMBEDDING_TIMEOUT` (20s) still governs steady state, where the same provider answers in **0.05s**. Judging both by one number rejected a working encoder and dropped the install to lexical retrieval permanently.
- A missing or unreachable encoder is remembered for `REMOTE_RETRY_SECONDS`, **doubling per consecutive failure** up to `REMOTE_RETRY_MAX_SECONDS`. Encoding happens several times per question, so retrying each call would make an offline machine feel broken — and a fixed window made a machine with no encoder pay the full timeout again every two minutes forever. The failure is stamped *after* the attempt: stamping it before shortened the window by exactly the cost of the failure.
- An encoder that loads but cannot encode is dropped rather than retried. `EMBEDDING_ALLOW_DOWNLOAD` is off, so a missing sentence-transformers model is not fetched inside somebody's question.
- `rank()` re-encodes a stored vector whose **width** differs from the query's. Changing encoder (384 → 768, or back to hashing) would otherwise score every stored row at exactly 0.0, silently emptying the semantic cache and trajectory memory rather than rebuilding them.

### Image size

Measured, not estimated — from PyPI wheel metadata for the pinned versions, and `du` for the frontend.

| Image | Was | Now | How |
|-------|-----|-----|-----|
| backend API | torch + 11 CUDA wheels = **2.8 GB** compressed, plus `build-essential` and a second copy of the analysis stack | none of it | embeddings via the provider; `requirements.txt` is the server only; every dep is a manylinux wheel so no compiler is needed |
| sandbox | `xgboost` alone = **154 MB** compressed (the wheel bundles CUDA) | `xgboost-cpu`, **4.5 MB**, same import name | plus `SANDBOX_TIER` layers |
| frontend | whole `node_modules`, **580 MB** | `.next/standalone`, **30 MB** | `output: "standalone"` |

The backend Dockerfile installs with `--compile` explicitly. `PYTHONDONTWRITEBYTECODE=1` is set for the runtime, and without the flag the image would ship no `.pyc` at all — meaning every new session re-parses the whole of pandas from source before it can answer anything.

## Testing

Four layers under `backend/tests/`: `unit/`, `integration/`, `regression/`, `negative/`.

Shared LLM stubs live in [tests/stubs.py](backend/tests/stubs.py) and the `stub_llm` fixture in `conftest.py`. `backend/tests` is on `sys.path` because `conftest.py` is there, so `from stubs import ScriptedLLM` works from any test module — do not cross-import between test files. Running out of scripted responses yields `"Done."` rather than raising, so a test fails on its own assertion instead of an IndexError that says nothing.

The loop changes the call *count*, not just the content: a `fast` run is plan → code → answer, an `auto` run adds a decision call per iteration plus a verification call. A test that scripts N responses and gets `"Done."` is usually missing the verification entry.

`regression/test_regressions.py` pins specific defects; each test's docstring states what broke and why. Read it before changing session handling, the database layer, the guard, the rate limiter, provider resolution or `sandbox.interrupt()`.

`regression/test_turn_cost.py` pins **what one turn is allowed to cost** — round-trip count per tier, per-purpose output budgets, that no chain of thought is re-read on a later call, and that a turn out of time still answers. These are not assertions about answer quality; they are the reason a question takes one minute instead of twenty. `RecordingLLM` records `max_tokens` per call for exactly this. Note that a streamed call must record **once**: `ScriptedLLM.stream_to` forwards its kwargs to `astream`, which is where recording happens.

The autouse teardown clears state through `semantic_cache.clear()`, **not** `db_mgr.clear_cache()` — `add()` writes to SQLite *and* to the in-process exact-match cache, and clearing only the table leaves a live entry that sends a later test with the same question down the cache-hit path. That failure is order-dependent and invisible when the file is run alone.

`conftest.py` also pins `WIZARD_CONFIG_DIR` to a temp directory — no test may read or write a developer's real credentials file — and `DATA_MODE=hybrid` with `DATA_SCHEMA_ONLY=false`, so each test states the mode and policy it means to exercise instead of inheriting a default that would refuse half the providers under test. The autouse teardown clears `usage_ledger` alongside the semantic cache, since it accumulates per session id.

It also pins `SKILLS_BUILTIN_DIR` and `SKILLS_PROJECT_DIR` to empty temp directories. `WIZARD_CONFIG_DIR` already covers the user layer, but the other two derive from the checkout and the working directory — so without this the suite's behaviour would depend on what Wizard happens to ship and on what the developer left in their own `.wizard/skills`. A test that wants a skill writes one. The teardown adds `db_mgr.clear_skill_candidates()` and `skill_registry.clear_user_skills()`: candidates deliberately have no `session_id`, so nothing else clears them, and an occurrence count carried forward fires the promotion threshold in a test that never asked a question twice. It also clears `install_index` and the staging root, which `clear_user_skills` does not touch — that removes skill *files* by path, and neither of those is one.

`SKILLS_REGISTRY_API` is pinned to `http://127.0.0.1:1` alongside the provider URLs. Every install test injects a fake fetcher through the `Fetcher` Protocol; the pinned port is what turns "somebody forgot to" into an immediate failure rather than a request to github.com from a test run.

`conftest.py` pins `OLLAMA_BASE_URL`, `LMSTUDIO_BASE_URL`, `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` to `http://127.0.0.1:1`. Model discovery is the one component that dials out on its own; port 1 is refused instantly instead of waiting on a connect timeout or resolving `host.docker.internal`, which is a real host on some dev machines.

## Conventions

Ruff line-length is 120 with `E501` disabled — the formatter owns line length.
