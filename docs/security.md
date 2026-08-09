# Security, Data Mode & Permissions

> Deep reference for the Wizard w2 security architecture, data modes, permission profiles,
> redaction, and credential storage.
> Concise rules live in [`backend/CLAUDE.md`](../backend/CLAUDE.md).

---

## Data Mode — `core/data_mode.py`

**This is the mechanism behind the local-first promise.** Before it, "your
data stays local" was a property of how somebody happened to configure their
`.env`.

`local-only` / `cloud-only` / `hybrid`, session-wide, seeded from
`settings.data_mode`.

**Enforcement lives in `LLMProvider.resolve`** — the one function all nine LLM
call sites already pass through. A violation raises `DataModeViolation`.

### Three Axes

- **Mode**: which providers a role may resolve to (`local-only` refuses cloud).
- **Policy** (`DataPolicy`): how much of the data a cloud-bound prompt carries.
- **Tools**: `web_search` is *unavailable* under `local-only`. `SEARCH:`
  directive is dropped with a warning. `disabled_tools()` feeds the UI.

Switching mode **clears any role assignment the new mode forbids**.

Embeddings are a role like any other — but a forbidden encoder **degrades to
the hashing fallback** instead of raising.

---

## Redaction — `should_redact` + `prompts.py`

`generate_system_context(..., redact=True)` keeps shape, column names, dtypes,
null rates and semantic types, and drops every real value. It adds a line
telling the model values were withheld.

**Decided per prompt, from the provider that prompt is going to**, not once per
turn. Under `hybrid` with a cloud manager and a local worker, the planner is
redacted and the code generator is not.

**Execution output is deliberately *not* redacted.** The answer is synthesised
from real stdout by `create_answer_prompt`; withholding it would leave the
answering model nothing to answer from.

Settable **per source** as well as per session (`DataPolicy.per_dataset`).
"Follow default" is a real third state. An override is dropped with its
dataset.

---

## Permission Profile — `core/permissions.py` + `agent/consent.py`

**Two independent dials.** Depth (`fast`/`auto`/`deep`) is how hard the agent
works; the profile (`auto-approve`/`ask-always`/`custom`) is how often it
stops to ask. **Data mode outranks the profile, always.**

### Categories

| Category | Live | Trigger |
|---|---|---|
| `library_install` | yes | `imported_modules(code) - runtime.missing_modules(...)`, checked **before** execution |
| `network` | yes | The plan's `SEARCH:` directive; installing a skill from GitHub |
| `workspace_write` | yes | A literal path the guard rejected, defaulting to `deny` |
| `db_connect` | yes | Opening a saved connection |
| `db_write` | yes | Writing a session table back to a source (`always_ask`, subject `connection:table`) |
| `tool_use` | **no** | Declared for a later milestone |

### Key Rules

- `db_write` carries `always_ask=True`: it never resolves to `allow` from a
  profile, and `set_ruling` raises rather than silently clamping.
- **Default is `ask-always`, not `auto-approve`.** Defaulting to auto-approve
  would have made an upgrade silently stop asking about web search.
- **The guard is not weakened.** A `workspace_write` grant records the
  directory in `PermissionState.extra_roots`; the code is then re-scanned.
  `GuardVerdict.only_paths` ensures a program mixing a path violation with a
  banned import is never offered for consent.
- Grants are session-scoped and **not persisted**. Tightening clears them.
- `AGENT_REQUIRE_APPROVAL` stays separate — that gate is about the plan,
  turn-terminating. It is wrong for an action chosen at iteration four, which
  is why `consent.py` exists.

### ConsentBroker

`ConsentBroker.ask` parks the turn on a future keyed by session id. Timeout,
cancel, disconnect and `abandon()` all resolve as denied. `orchestrator.run(can_prompt=...)`
is how a transport declares it has a reply channel; REST and CLI pass `False`.

A denial does **not** end the turn. `_act_code` records a failed `Step` and
returns, so the loop routes around it.

`chat.py`'s receive loop no longer awaits the run — an `approval` frame with
an `id` is routed to the broker before the "a run is already in progress"
check.

---

## Credentials — `core/credentials.py`

Keys live in `credentials.json` under the platform config directory
(`utils/appdirs.py`). `WIZARD_CONFIG_DIR` overrides it; the test suite pins it.

This is not encryption at rest — the guarantee is the OS's access control.
OS keychain integration is deliberately not taken (three platform backends plus
a dependency, and Secret Service is often absent on headless Linux).

### File Permissions

- POSIX: `0600`.
- Windows: inheritance stripped, single-user ACL via `icacls` granted to the
  **SID from the process token** (`whoami /user`), never `%USERNAME%`.
  Result verified afterwards; rolled back to inherited permissions if the file
  came back unwritable.

Resolution order: **environment/settings first, then the store.** Keys are
never logged and **never returned by any route**.
