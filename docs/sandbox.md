# Sandbox & Isolation

> Deep reference for the Wizard w2 sandbox and isolation layers. The concise
> rules live in [`backend/CLAUDE.md`](../backend/CLAUDE.md); this file
> explains the full enforcement model.

---

## Docker Backend — `tools/sandbox.py`

`SandboxPool` creates **one container per session**, lazily.

- The daemon records its own PID; `interrupt()` signals it directly. Signalling
  PID 1 would kill the container, since PID 1 is `sleep infinity`.
- Limits: `mem_limit`, `pids_limit`, optional `cpu_quota`, `cap_drop=ALL`,
  `no-new-privileges`, plus a socket deadline per execution. The daemon's own
  `RLIMIT_AS` is deliberately left off here — Docker already enforces the
  ceiling, and a soft limit inside a hard-limited cgroup turns an OOM kill into
  a confusing `MemoryError`.
- The image tag carries the tier (`settings.sandbox_image`), so switching
  `SANDBOX_TIER` builds a new image instead of reusing one with different
  libraries.

---

## OS Sandbox — `security/sandbox/`

With Docker opt-in, this is what stands between generated code and the machine
on a default install. The AST guard is unchanged and still runs first; this is
the layer beneath it.

`HOST_SANDBOX` is **`off` / `best-effort` / `require`**, defaulting to
`best-effort`. Three states rather than a bool because a silent downgrade and a
refusal are both wrong as a universal answer.

`HOST_SANDBOX_NETWORK` (`deny`/`allow`, default `deny`) governs *outbound*
traffic only — loopback is always permitted, because the daemon protocol is a
loopback socket.

### Module split

Split so the majority is testable without spawning anything:

| Module | What it is |
|---|---|
| `policy.py` | `SandboxPolicy` — inert, JSON-safe data. The single description of the boundary. |
| `profiles.py` | Generates the macOS SBPL profile as a pure function. |
| `capability.py` | What this machine can enforce, per feature, with a reason for every gap. |
| `spawn.py` | Decorates the launch. Returns a `SpawnPlan` rather than spawning. |
| `child.py` | The only part that restricts a live process. Loaded by file path, imports nothing from `src`. |
| `selftest.py` | Spawns a probe that tries to escape and reports what stopped it. |

### Two-phase enforcement

The daemon binds a loopback TCP listener, and a filter denying `socket()`
cannot be installed before it:

1. **`apply_policy`** — runs before the daemon (filesystem, memory,
   no-new-privs).
2. **`seal_network`** — runs after `listen()`. `accept()` on an already-bound
   descriptor makes no `socket()` call, so the connection survives.

The bootstrap leaves the seal on `builtins.__wizard_seal__`; the daemon calls
it and returns the result through `capabilities`, and `HostSession` logs that
report at start.

**Both halves go through `import builtins`, never `__builtins__` global.**
`runpy.run_path` binds `__builtins__` in the daemon's globals to a *dict*, so
`getattr` finds nothing on it: the seal was silently skipped on every real
session while the self-test still reported the network blocked. A test pins
the spelling.

### Per-platform details

#### Linux

`PR_SET_NO_NEW_PRIVS`, then Landlock (syscalls 444/445/446), ABI probed via
`create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` and the
handled-access set masked to what that ABI admits. Then a seccomp-bpf filter
refusing `socket()` for `AF_INET`/`AF_INET6`/`AF_PACKET`/`AF_NETLINK`.

#### macOS

A deny-by-default SBPL profile via `sandbox-exec`. Availability is **probed**,
not inferred from an OS version: it has carried a deprecation warning since
10.14 and still works. The documented replacement (App Sandbox entitlements)
needs a signed app bundle.

#### Windows

A job object supplying `ProcessMemoryLimit`, `ActiveProcessLimit` and
`KILL_ON_JOB_CLOSE`. This closes the `RLIMIT_AS`-on-POSIX-only gap. Plus a
Low integrity level the child applies **to itself**. Reads keep working under
the no-write-up policy; the workspace is labelled Low via `icacls`.

Assignment to the job happens just after spawn rather than through
`CREATE_SUSPENDED`, because `subprocess` closes the thread handle — a
microsecond gap during interpreter startup.

**Network is not enforced on Windows** and `capability.detect()` says so with
the reason: WFP needs administrator, and AppContainer would require
re-ACLing the user's Python installation.

### Cache and grant handling

Matplotlib, fontconfig and pip cache under the user's home, which no writable
root covers — so `policy.cache_environment()` redirects `MPLCONFIGDIR`,
`XDG_CACHE_HOME`, `TMPDIR` and friends into the workspace.

A `workspace_write` grant from the permission profile widens the **sandbox**
as well as the guard. A Landlock ruleset cannot be widened after
`restrict_self`, and neither can an SBPL profile or a lowered token — so a
grant at iteration four goes through `runtime.rebind_roots`, which restarts
the child. The daemon reloads datasets and tables; what a restart costs is
intermediate variables, not data.

### Selftest

`GET /api/sandbox/selftest` spawns a child through the real machinery and has
it attempt each forbidden operation. Outcomes are `blocked` / `allowed` /
**`inconclusive`** — the probe dials a TEST-NET address (RFC 5737), so a
timeout proves nothing either way. A feature the platform reports as
unsupported does not fail the verdict.

---

## Host Backend — `tools/host_runtime.py`

`HostRuntimePool` creates **one subprocess per session**, lazily. Same daemon,
same protocol, no image. This is the default backend.

- `RLIMIT_AS` from `HOST_RUNTIME_MEM_LIMIT` on POSIX. **Windows has no
  equivalent without pywin32** — do not claim otherwise in the UI.
  `LOCAL_RUNTIME_*` is still accepted as an alias.
- Interrupt: `SIGINT` on POSIX, `CTRL_BREAK_EVENT` on Windows — which needs
  `CREATE_NEW_PROCESS_GROUP` at spawn, or the signal reaches the API process.
- `get()` restarts a child that has exited (OOM kill, crash).
- Runtime pip inside the daemon is off by default here: it would install into
  the backend's own environment. **On-demand installs happen in the parent**
  — `tools/packages.py` runs `pip install --target <workspace>/.libs`, which
  the daemon has on `sys.path` and re-scans with `importlib.invalidate_caches()`.
  Two reasons: inside a sandboxed child the install sits behind a network
  policy that denies it, and `library_install` gate runs in the parent.
  `SANDBOX_ALLOW_RUNTIME_PIP` is the master switch.
