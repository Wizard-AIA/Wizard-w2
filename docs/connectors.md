# Connectors

> Deep reference for the Wizard w2 connector system. The concise rules live in
> [`backend/CLAUDE.md`](../backend/CLAUDE.md); this file explains the full
> design.

---

## Overview — `core/connectors/`

Databases, document stores and object storage, as an ingest source **parallel
to file upload**. The deliverable is the interface, not a list of supported
engines: which databases an install can reach is a question of which driver is
installed.

---

## Snapshot, Not Live Pushdown

A connection is read in the *parent* process and registered through
`Session.add_dataset`, which materialises `workspace/tables/<table_key>.feather`
for the daemon exactly as an upload does. Generated code never holds a
connector and never opens a socket, so `HOST_SANDBOX_NETWORK=deny` keeps
meaning what it says — and the write gate is real rather than advisory, because
a write is a parent-side action the agent has to ask for.

`fetch(query)` exists on the protocol and is parent-side only. A regression
test asserts no DSN or secret reaches the workspace.

---

## Module Split

Split like `security/sandbox/`, so most of it is testable with nothing running:

| Module | What it is |
|---|---|
| `spec.py` | Inert, JSON-safe data — this layer's `policy.py` |
| `base.py` | The `Connector` Protocol |
| `registry.py` | This layer's `providers.py` |
| `store.py` | Connection persistence |
| `gate.py` | Consent orchestration |
| `ingest.py` | The import flow |
| Three reference drivers | `relational.py`, `document.py`, `objectstore.py` |

**SQLite is what makes it testable** — no third-party driver, so the suite
exercises the real connector against a real database offline.

---

## Registry

- **Keyed by an explicit `kind`, not by a URL scheme sniffed from the DSN.**
  The two look equivalent and are not: `s3://` and `mongodb+srv://` are not
  SQLAlchemy dialects, an object store often has no scheme at all.
- Adding a connector is a `register()` call and a module; a unit test
  registers one from outside the package.
- Drivers are probed with `find_spec`, never imported — this renders on every
  page load. A missing driver is **listed with the pip command**, not hidden.

---

## Relational Driver

`relational.py` builds its bounded read as `select().limit()` off a reflected
table rather than assembling SQL. The row limit is spelled differently per
dialect; reflection also means the identifier is never interpolated.

---

## Persistence and Credentials

**A connection is configuration; the tables it imports are data.**

- The non-secret half persists to `connections.json` in the platform config
  directory. The secret half goes to `credential_store` under
  `connection:<id>`.
- `ConnectionSpec` carries a *reference* to a credential, never the credential
  — so there is no field to forget to strip.
- Both files go through `utils/fileperms.restrict`, shared because the Windows
  ACL was already got wrong once.
- `providers_with_keys()` filters out anything containing a colon. Without it
  a saved database password is reported as a configured model provider.

---

## Dataset Naming

`spec.dataset_name()` is the only thing that builds the table key. A table
named `public.orders` would have stem `public` via `Path(name).stem` — every
table from one connection collapsing. A test pins two tables from one
connection staying distinct.

`DatasetHandle.origin` names the connection a table came from, so one
data-policy decision covers every table from a source. Passed to
`should_redact` as its own argument rather than split back out of the name.

`CONNECTOR_MAX_ROWS` is not optional. Truncation is reported through
`profile.truncated`/`original_rows`.

---

## Consent — `connectors/gate.py`

`orchestrator._permit` needs a `RunState`, an emitter and a socket; a user
clicking Import has none. So the REST sibling adds one rule: **an
authenticated request from the user is itself the answer to an `ask`**, and
it records the grant so the agent inherits the answer.

`deny` stays terminal — it is a real third state, not a stronger `ask`.
What is gated is not *saving* a connection (which reaches nothing) but
**opening** one.

### Write-back

Three independent locks:
1. `spec.read_only` off for that connection, with its name typed back
   (checked first, **without asking anything**).
2. `db_write` not denied.
3. A grant recorded per `connection:table` — approving `staging.results` is
   not approving `prod.orders`.

`db_write` keeps `always_ask`, so no profile reaches it. Enabling write-back
does **not** grant a write.

`db_connect` and `db_write` flipped to `live=True` **in the same change that
gave them call sites**.

---

## Data Mode Interaction

`network` gates installing a skill from GitHub, not connectors. Connectors are
gated under `db_connect` — see the permission profile section in
[`architecture.md`](architecture.md).
