# Agent Loop & Orchestration

> Deep reference for the Wizard w2 agentic loop, orchestration, subagents, and trust layer.
> Concise rules live in [`backend/CLAUDE.md`](../backend/CLAUDE.md).

---

## One Request Path

`POST /api/chat` and `WS /ws/chat` both call `AnalysisOrchestrator.run`. The
transport only translates events into frames — it contains no workflow logic.

Historically the WebSocket handler re-implemented the node loop by hand, and
the two copies drifted until the semantic cache and the fast-path router
applied to REST only.

---

## Event Protocol — `events.py`

The orchestrator knows nothing about WebSockets; it emits typed events to an
`Emitter`. `EventCollector` buffers them for the REST path and for tests;
`WebSocketEmitter` serialises them onto a socket.

### Frame catalogue

| Frame | Notes |
|---|---|
| `session` | Session metadata handshake |
| `status` | Progress indicator update |
| `step_start` / `step_end` | Granular action execution bounds |
| `reasoning_delta` | Split from `plan_delta` during streaming by tracking the `<thought>` tag boundary incrementally (`_stream_plan`) |
| `plan_delta` | Incremental plan text streamed to the UI |
| `content_delta` | Direct text generation chunk |
| `code` | Code block generated for execution |
| `stdout` | Real stdout captured from runtime |
| `artifact` | Output table, plot, or exported script |
| `approval_required` | Carries an `id` **only** for a mid-run permission gate — tells client the turn is paused in-place |
| `warning` / `error` | Diagnostic / failure alerts |
| `final` | Terminal response payload |
| `iteration_start` | Marks beginning of an agentic loop round |
| `action` | Step chosen by manager or deterministic fallback |
| `observation` | Closes the most recent `action` that has none (never correlated by id) |
| `finding` | Extracted analytical insight |
| `plan_revised` | Updated plan following unexpected data observation |
| `assumption` | Extracted analytical assumptions |
| `verification` | Independent check outcome (`VERIFIED:` / `MISMATCH:`) |
| `skill` | Names which skill informed the turn |
| `skill_candidate` | Offer to save analysis (`recurring` vs `recovery`) |
| `usage` | Emitted **only when a cloud model ran** (silence under `local-only`) |
| `SUBAGENT_START` / `SUBAGENT_END` | Bound a branch's lifetime for UI; nested events tagged by `BranchEmitter` |

---

## Workflow — `orchestrator.py`

**This is a loop, not a pipeline.** Each iteration the manager sees what has
actually run and chooses the next move; the run ends when it says it can
answer, or the budget is spent.

```
orient (plan) → [plan gate] → loop → verify → answer
                                ↑ ↓
                 inspect / code+execute / consult / reflect
                          ↓         ↓
              [permission gate]  correct   (bounded by MAX_CORRECTION_RETRIES)
```

### Gates

- **Plan gate**: ends its turn, resumed by starting a new one. Opt-in
  (`AGENT_REQUIRE_APPROVAL`). A plan containing `SEARCH: "…"` halts for
  consent regardless. Under `local-only` search is **refused** rather than gated.
- **Permission gate**: **suspends** and resumes in place via `ConsentBroker`.
- An approved plan skips `_orient` entirely (where the gate lives), so it
  cannot re-fire. It must also not be downgraded to `fast`.

### Actions

Actions live in `actions.py`. `parse_decision` **never raises** — malformed
model output resolves to a default (`code` mid-run, forced `answer` on the
last iteration). `inspect` is answered deterministically from the frame
(`Session.inspect`), costing no LLM call.

### Budgets and Modes

- Budgets come from `settings.budget_for(mode, parameter_size)`.
- Modes: `auto` (agent picks depth), `fast` (one shot, **no verification**),
  `deep`. `planning` is a legacy alias meaning "deep, gate the plan".
- **Below the balanced tier the loop decides deterministically**
  (`TierBudget.allow_decisions=False`). `_decide_deterministically` reads the
  answer off what happened: a step that succeeded and printed something means
  stop, anything else means write code. Turns a nine-call compact turn into a
  three-call one. `deep` restores the round-trip on every tier.
- The same deterministic function is the *default* on every tier when the
  model's answer is unparseable.
- `AGENT_TURN_TIMEOUT` is checked **before an iteration is claimed** — never
  mid-call, not after `iterations_used` is incremented. Verification is the
  first thing it gives up.

### Motivation (DABstep)

The old shape fixed a plan before touching the data and fed 200 characters of
each step's output into the next. It could not recover when the data
contradicted the plan. [DABstep](https://arxiv.org/abs/2506.23719) measures
the gap: hard tasks need 6+ dependent steps, and planning is the largest error
category.

---

## Subagents — `orchestrator.py`

`parallel` is a fourth kind of step alongside `code`/`consult`/`reflect`: the
manager fans one step out into several concurrent, isolated mini-investigations.

Offered only when `settings.SUBAGENT_ENABLED and budget.allow_subagents and
budget.max_subagents >= 2` — the compact tier never sees it.

### Key Implementation Details

- **Real concurrency needs one runtime per branch.** The daemon protocol is
  single-in-flight per process (one `accept()` loop, one shared `exec_globals`,
  a process-global stdout swap). `Session.spawn_subagent_id` mints a composite
  id (`f"{session.id}{CHILD_DELIMITER}{branch}"`) that gets its own runtime,
  workspace and usage-ledger bucket.

- **`SubagentSession` is a structural proxy, not a subclass.** It overrides
  `.id`, `.executor`, `.workspace`; everything else forwards to the parent
  through `__getattr__`. Concurrent branches share one `PermissionState` —
  two branches can both prompt for the same subject before either grants.

- **`_split_subgoals` splits on `\s+\|\s+`, not a bare `|`.** A goal like
  "count rows where status matches A|B" would otherwise read as two sub-goals.
  Fewer than two real sub-goals degrades to a plain `_act_code` step.

- **Child id is qualified by `group`** (`f"{group}-sub{index+1}"`), not just
  the branch label — a second `parallel` decision reuses `"sub1"` and without
  the group prefix that would collide.

- **Each branch runs a bounded, deterministic mini-loop** (`_run_subagent`,
  reusing `_act_code`) — no decision or verification round-trip inside it.
  The parent verifies once at the end.

- **`inprocess` runs branches serially, not concurrently.** That backend has
  no per-call isolation. Real backends (`host`, `docker`) run branches through
  `asyncio.gather` under a shared deadline.

- **Cost rolls up automatically.** Every LLM call books under its composite
  id, so `usage_ledger.totals_many([session.id, *state.subagent_ids])` is what
  makes the readout include subagent spend. `release_subagent_runtime` frees
  a branch's process immediately but does **not** forget its usage-ledger entry;
  full teardown (`dispose_subagent`) waits for the whole turn to end.

---

## Trust Layer — `grounding.py`

Deterministic, no LLM calls, and it **reports rather than edits** —
post-processing model output is an anti-pattern.

- `check_grounding` flags numbers in the answer that appear in no execution
  output. Tolerance comes from the *answer's own* precision (`3.14` for an
  output of `3.14159` is reporting, not invention) plus magnitude words
  (`1.23 million`).
- `assumptions_from_code` reads silent decisions back out of the code that
  ran — `dropna`, `how='inner'`, `nlargest`, `errors='coerce'`. Each changes
  what the number means.
- `_verify` re-derives the headline result by a different route and looks for
  `VERIFIED:` / `MISMATCH:`. A wrong join grain produces a confident,
  plausible, wrong number that no self-review catches.

---

## Export — `agent/export.py` + `routes/export.py`

Turns a turn's *real executed steps* — pulled from the investigation, never
reconstructed from the model's description — into a runnable script or
notebook.

- **Two callers, one builder.** The always-on per-turn artifact
  (`orchestrator._write_script`) and the on-demand `GET /api/export/{message_id}`
  route both go through `export.build_script`/`build_notebook`.
- **Two loader shapes.** The always-on artifact reads Feather files in place;
  the on-demand export ships CSV copies (`bundle_files`) and reads those.
  `dataset_loader_lines` takes the file template and pandas reader as
  parameters.
- **A connector-sourced table is never embedded.** It is looked up by name at
  run time through `ConnectionStore.by_name`.
- **Chat messages persist each turn's real steps** (`chat_messages.meta`)
  specifically so a turn stays exportable after a later turn overwrites the
  workspace's single `analysis.py`.
- **A zip, not a bare file, whenever a file-based table needs to travel with
  the script.** The route decides per export.
- **User-initiated only**, from the results UI.
