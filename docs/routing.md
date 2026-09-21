# Turn routing

How Wizard decides how much machinery a message needs. Introduced in v1.0.14.

Before this, every message entered one pipeline: plan, loop, verify, answer. So
`hi` after an analysis planned, wrote code and ran it. The pipeline was correct
for a hard question and wrong for nearly everything else a person says.

The rule that replaced it: **architecture constrains what is unsafe or invalid;
it does not force every message through a workflow that does not match what the
person meant.** Safety stays fixed (CodeGuard, sandbox, consent, data mode). The
shape of the work is chosen per message.

Code: [`backend/src/core/agent/routing.py`](../backend/src/core/agent/routing.py)
(the router), [`conversation.py`](../backend/src/core/agent/conversation.py)
(the chat path), [`orchestrator.py`](../backend/src/core/agent/orchestrator.py)
(`_decide_route`, `_converse`, `_run`).

## What happens to one message

```
message ──► route_turn(message, session facts, mode)      pure, no model call, no I/O
              │
              ├─ converse   1 model call, no dataset access, no tools
              │     └─ reply is the sentinel? ──► re-route the SAME turn as analysis
              ├─ inspect    dataset facts read from the frame, 1 model call to write them up
              ├─ direct     code ─► run ─► answer                      (no planner, no verify)
              ├─ agentic    [plan] ─► loop(decide/code/run/…) ─► [verify] ─► answer
              ├─ plan_only  plan ─► stop and wait for the user
              └─ execute_plan  run the plan the user confirmed
```

Every turn ends in exactly one terminal frame: `final`, `error`, a plan-gate
`approval_required` without an `id`, or `cancelled`.

| Message | Route | Model calls |
|---|---|---|
| `hi`, `thanks`, `what can you do?` | converse | 1 (two if it turns out to need the data) |
| `what columns are in this?`, `how many rows?` | inspect | 1 |
| `what is the average salary?` | direct | 2 (code, answer) |
| `plot salary by department` | direct | 2 |
| `why did churn go up, and which segments drive it?` | agentic, planned, verified | 5 or more |
| `make a plan for the churn analysis` | plan_only | 1 (the plan) |

Counts are what the behaviour tests pin, with a scripted model
(`backend/tests/integration/test_turn_behavior.py`).

## How it decides

Routing reads **evidence**, not keywords. `extract_signals` reads whole-token
facts from the message and the session: which of the dataset's column names the
message mentions, which operation families it asks for (aggregate, transform,
model, and so on), whether it asks for something investigative or statistical,
whether it has a filter or a threshold, how many dependent steps it lists,
whether it addresses Wizard's last answer, whether it asks for a plan. Each is a
weighted fact; none is a verdict.

- **No task evidence means conversation**, whatever the wording. That is why
  `cheers` and `lol nice` work without being on a list.
- Tokens match on word boundaries, so `hi` does not fire inside `which`.
- Complexity is the number of independent *steps*, not the length of the
  message: two aggregations are one step, an aggregation plus a model plus a
  report are three. Thresholds: 3.0 is complex, 1.5 is moderate.
- The router never sees a previous task. `Session.turn_context()` hands it plain
  values (has a dataset, its column names, whether an earlier turn exists,
  whether a plan is waiting), so a finished analysis cannot define what `hi`
  means.

### The uncertain band: converse or escalate

When a message is probably conversation but could be a request (or is in a
language the signals do not know), the route is `converse` with
`escalate=True`. The conversational reply is itself the classifier: the model
answers normally, or replies with the sentinel `[[NEEDS_ANALYSIS]]` alone.
`EscalationGate` hides the sentinel from the stream, and the same turn is
re-routed as analysis (a second `route` frame with `source: "escalation"`). No
separate router model is called. With no dataset loaded there is nothing to
escalate to, so the reply says what is missing (`needs_data`).

## Modes are policy

Auto, Fast and Deep are a user preference that stays until changed. They change
how hard Wizard works on an analytic message. They never change what the message
is (`apply_mode`).

| Mode | Analytic message | Conversation, inspect |
|---|---|---|
| Auto | The router's choice | The router's choice |
| Fast | No planner, no verification, one pass | Unchanged |
| Deep | Full investigation with verification, deep budget | Unchanged |
| Plan (legacy wire value `planning`, and `AGENT_REQUIRE_APPROVAL`) | The plan is confirmed by the user before anything runs | Unchanged |

An explicit request for a plan is honoured in every mode: it is an instruction,
not a preference. A greeting costs one call in all four modes.

## State: what may carry over to the next message

Four kinds of state exist. Only some may influence routing.

| Kind | Where | May affect the next message's route? |
|---|---|---|
| Conversation history | `chat_messages` | Read for context in replies; never decides a route |
| Task state | `Session.task` (`TaskState`) | Only a waiting plan and the last chart code, both explicit |
| Turn/UI state | Frontend `TurnContext`, per-message `phase` | No |
| User preference | Mode, permission profile, model choices | Mode only, as policy |

`Session.task` holds `status`, `pending_plan`, `pending_instruction` and
`last_code`. The orchestrator owns it: `begin_turn` when a turn starts,
`end_turn` when it ends (in a `finally`-equivalent, so a cancel or a crash leaves
nothing running). The transport never begins or ends a turn. A waiting plan is
kept with the instruction it was made for, so typing "go ahead" runs that plan
against that question, not against the words "go ahead". Adding, switching or
removing a dataset resets the task. A cancelled turn does not clear the chart
from the turn before it.

The semantic cache stores a solution only from a turn that produced code and did
not fail, and its key includes the flow (`direct` or `agentic`). Working memory
learns only from a turn that produced code. A greeting is never cached and never
taught to anything.

## Frames

`status {phase: "routing"}` is the first frame of a turn. `route` follows, with
`intent`, `workflow`, `complexity`, `plan`, `verify`, `escalate`, `deep`,
`needs_data`, `source` (`rules | mode | escalation | approval`), `reasons` and
`mode`. `final` carries the same `route` and a `status`; `POST /api/chat`
returns it as `ChatResponse.route`.

`error.code` is one of `busy`, `llm_unavailable`, `data_mode`,
`session_not_found`, `internal`, `no_dataset`. A `busy` error refuses the new
message and does not touch the turn already running. `cancelled` carries a
`reason` of `user` or `disconnect`.

A message sent while a turn runs is checked first for interrupt intent: a
whole-message "stop", "cancel" or "never mind" cancels the running turn. Anything
else is `busy`, and the frontend hands the text back to the composer.

## Generation budgets

Every model call in a turn resolves its output limit through
`AnalysisOrchestrator._budget`, which calls `resolve_generation`. Precedence and
provider adapters are in [llm.md](llm.md#generation-configuration).

## Debugging a route

Each turn writes one `Turn trace` log line at info level: route (intent,
workflow, complexity, plan policy), mode, whether a planner ran, whether it
escalated, models used, actions taken, the output budget per purpose, prompt
characters, model call count, latency and how it ended. It contains no message
text and no prompt text.

To see why a message routed as it did, read `route.reasons` on the `route` frame
(the UI shows them on hover), or run the router directly:

```python
from src.core.agent.routing import TurnContext, route_turn

ctx = TurnContext(has_dataset=True, columns=("salary", "department"))
route = route_turn("average salary by department", ctx, "auto")
print(route.workflow, route.reasons)
```

## Known limits

- **Web search is reachable only through a planner turn.** `SEARCH:` is offered
  to the planner and nowhere else, so a direct or fast question never searches.
  Ask in Deep, or ask for a plan, when a question needs outside knowledge.
- **The signals are tuned for English.** A message in another language usually
  carries no signals the router knows, so it lands in converse-or-escalate and
  the model decides. That costs at most one small call, but it is a call.
- **Routing is a heuristic over evidence.** It will sometimes choose a heavier or
  lighter route than you would. A wrong light route is recoverable in the same
  turn (escalation, or ask again in Deep); a wrong heavy route only costs time.
  The behaviour suite pins the cases that matter (`test_routing.py`,
  `test_turn_behavior.py`).

## Checking it against a real model

Every test above uses scripted models. `scripts/live_acceptance.py` drives the
real app over its real `/ws/chat` socket with a real provider, in `cloud-only`
mode on a synthetic table, in the order that used to fail (an analysis, then
`hi`). It checks what was routed and which frames ran, never the wording of an
answer.

```bash
# The key goes in through stdin only: not argv, not a file, not the repo.
pbpaste | .venv/bin/python scripts/live_acceptance.py --model gemini-2.5-flash
```

Gemini's free tier allows about five requests a minute per model, and a complex
analysis turn makes several, so the script waits between turns (`--pace`) and
retries a rate-limited turn once. Run without a model it still verifies the
routing (a fake key fails only the turns that need an answer).
