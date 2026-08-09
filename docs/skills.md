# Skills

> Deep reference for the Wizard w2 skill system. The concise rules live in
> [`backend/CLAUDE.md`](../backend/CLAUDE.md); this file explains the full
> design.

---

## Overview — `core/skills/`

Reusable know-how the agent can **cite**. Everything else it remembers is
private and opaque — the semantic cache, working memory and trajectories all
change what it does with nothing anyone can read, edit or name. A skill is a
`SKILL.md` file: frontmatter plus instructions.

---

## Module Split

| Module | What it is |
|---|---|
| `spec.py` | Inert data |
| `frontmatter.py` | Pure parser |
| `loader.py` | One file |
| `registry.py` | The layered store |
| `promotion.py` | The candidate pipeline |

---

## Three Layers (Ascending Precedence)

**Built-in → user-global → project.** A name defined twice resolves to the
more specific layer, and the shadowed copy is still listed with `shadowed_by`
set. Built-in lives in the checkout and is **read-only**: `write`/`delete`
raise `SkillNotWritable` and the API 409s. The documented override is a user
skill of the same name.

**The user layer is `config_dir()/skills`**, not `~/.wizard/skills`. The spec
writes the latter; `utils/appdirs.config_dir()` is the single answer for
user-level state.

---

## Trust Boundary: No Executable Code

**A skill may never carry executable code.** Enforced in `loader.load_skill`:
a skill directory containing `.py/.sh/.ps1/.bat/.exe/.so/...` is **refused,
naming the file**. Python inside a skill body is illustrative text; the only
way anything derived from it executes is the worker writing code that passes
`CodeGuard.scan` and runs in the sandbox.

---

## Frontmatter

`frontmatter.py` is a **restricted YAML subset**, not PyYAML — which would
return aliases and nesting nothing downstream expects. A parser that can only
produce strings and lists of strings cannot be talked into any of it. Anything
outside the subset raises, naming the line.

---

## Retrieval

**Planning prompt + the `consult` action. Not the worker prompt.** The worker
prompt is rebuilt per iteration *and* per correction retry, so a block there is
paid for N times. The plan, which is what the skill informed, already rides
along. `regression/test_turn_cost.py` pins all of it, including that the block
never exceeds `SKILLS_MAX_CHARS`.

Retrieval costs **no LLM call**: it is a ranking over local files.

### Scoring

Without an encoder, ranking is **question-coverage**, not the hashing
encoder's cosine. Coverage — what fraction of the question's content words the
passage contains — gives correct ordering and lands on the same scale as a
transformer's cosine, which is why one `SKILLS_MIN_SIMILARITY` serves both.
Normalised by the *query*, not the union (unlike `retriever.lexical_overlap`).

`consult` is offered when the session has documents **or** skills are installed.

---

## Promotion — `skills/promotion.py`

Two kinds counted **separately**: `recurring` (a successful turn whose question
recurs) and `recovery` (a failure-then-fix that has recurred). Threshold is 3.

### Key rules

- **A cache hit counts as recurrence** — the cache short-circuits the same
  question, so the second and third times are exactly when nothing is
  re-derived. Skipping them left the counter permanently at one.
- A cached turn must **not** overwrite the stored draft.
- The offer is emitted **exactly once**, at the threshold. A dismissed or
  promoted candidate still participates in matching.
- `skill_candidates` has **no `session_id`** — "you keep doing this" is a
  claim about many sessions. `delete_session_data` does not touch it.
- **Nothing writes a skill automatically.** A file appears only when the user
  confirms. A draft is never asked of a model.

### Two routes into promotion

1. **Threshold offer** — the agent's; from the candidate pipeline.
2. **`POST /api/skills/draft`** — the user's; "save *this* one", no threshold.

`POST /api/skills` settles a candidate only when given a `candidate_id`.

---

## Skill Usage

`skill_usage` answers "which analyses used which skill". One row per skill per
turn. Recorded **outside** the success branch. Like `skill_candidates` it has
no `session_id`.

**Reading and writing a local skill is not a permission category.** Installing
one from GitHub is, under `network`.

---

## Installing from GitHub — `skills/install.py`

Four modules: `source.py` (URL parsing), `fetch.py` (the only thing that dials
out), `index.py` (local provenance), `install.py` (the flow).

### Security order

**Parse → resolve to a commit → stage → the user reads it → approve.** Nothing
reaches the agent between stage and approval.

### Key rules

- **The Contents API, not a tarball.** `GET /repos/{o}/{r}/contents/{path}?ref=<sha>`
  returns JSON. The executable-payload refusal is enforced from the listing,
  before a byte of content is fetched.
- The rule itself is `loader.offending_names`, extracted so the on-disk check
  and the remote check apply the *same* function.
- **Pinning is its own step.** The ref resolves to a SHA once and every later
  request carries it.
- **Provenance is what we wrote, never what was fetched.** The loader ignores
  `source_url` and `pinned_sha` from frontmatter; `install_index.overlay`
  stamps them from `installed.json`.
- **Staged into `config_dir()/skills-pending/`** — a sibling of the user root,
  not inside it. The registry never scans it.
- **Pin, don't track.** `check_update` re-resolves the stored ref. The diff is
  against the file on disk, not against upstream at install time.
- **A gist request carries the revision in its path** (`gists/{id}/{sha}`).
- **Gated by `network`, not refused by `local-only`.** No session data leaves.
  `OUTBOUND_TOOLS` is scoped to tools the agent invokes mid-analysis.
- **Install is user-initiated only — never an agent action.** A fetched skill
  is untrusted text that goes into the manager's prompt; if the manager could
  also install skills, a fetched skill could instruct the agent to fetch more.
  That is wormable.
- The optional token lives in `credential_store` under `registry:github`.
- `SKILLS_REGISTRY_API` exists so GitHub Enterprise is a setting rather than a
  fork.
- **The CLI ships now**: `python backend/main.py skills add|list|update|discard|remove|token`.
  `discard` is separate from `remove`: they act on different things.
- `conftest.py` pins `SKILLS_REGISTRY_API` to `http://127.0.0.1:1`.
