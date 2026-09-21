import { test } from "node:test"
import assert from "node:assert/strict"

import { applyFrame, type TurnContext } from "./turn-controller.ts"
import { blankAssistant, blankUser } from "./turn-state.ts"
import type { ServerEvent } from "./types.ts"

/** A context with one running turn: a user message and the assistant message it writes into. */
function running(): TurnContext {
  const assistant = blankAssistant()
  return {
    messages: [blankUser("calculate the total"), assistant],
    activeId: assistant.id,
    running: true,
    phase: "routing",
    pending: null,
  }
}

const idle = (): TurnContext => ({ messages: [], activeId: null, running: false, phase: "idle", pending: null })

const frame = (event: Record<string, unknown>) => event as unknown as ServerEvent

test("the session frame is handled with no turn running", () => {
  // It arrives at connect, before any turn. Dropping it left uploads in a
  // different session from the one the chat socket talks to.
  const { ctx, effects } = applyFrame(idle(), frame({ type: "session", session_id: "abc" }))
  assert.deepEqual(effects, [{ kind: "session", id: "abc" }])
  assert.equal(ctx.messages.length, 0)
})

test("usage is recorded once whether or not a turn is running", () => {
  const event = frame({ type: "usage", calls: 2 })
  assert.equal(applyFrame(idle(), event).effects.length, 1)
  const during = applyFrame(running(), event)
  assert.deepEqual(during.effects.map((e) => e.kind), ["usage"])
  assert.equal(during.ctx.running, true)
})

test("the terminal frame ends the turn from the context it is given", () => {
  // The old handler compared against a stale `false` and never reset running.
  const { ctx } = applyFrame(running(), frame({ type: "final", response: "done", route: { workflow: "direct" } }))
  assert.equal(ctx.running, false)
  assert.equal(ctx.activeId, null)
  assert.equal(ctx.phase, "idle")
  assert.equal(ctx.messages[1].phase, "done")
  assert.equal(ctx.messages[1].content, "done")
  assert.equal(ctx.messages[1].route?.workflow, "direct")
})

test("frames after the terminal frame are ignored", () => {
  const ended = applyFrame(running(), frame({ type: "final", response: "done" })).ctx
  const late = applyFrame(ended, frame({ type: "content_delta", content: "stale" }))
  assert.equal(late.ctx, ended)
  assert.deepEqual(late.effects, [])
})

test("an error with no turn running still shows up as a failed message", () => {
  const { ctx } = applyFrame(idle(), frame({ type: "error", content: "Invalid or missing API key." }))
  assert.equal(ctx.messages.length, 1)
  assert.equal(ctx.messages[0].phase, "failed")
  assert.equal(ctx.messages[0].error, "Invalid or missing API key.")
  assert.equal(ctx.running, false)
})

test("a lost session clears the stored id and fails the turn, by code or by legacy text", () => {
  for (const event of [
    { type: "error", code: "session_not_found", content: "gone" },
    { type: "error", content: "Session not found. Start a new one." },
  ]) {
    const { ctx, effects } = applyFrame(running(), frame(event))
    assert.ok(effects.some((e) => e.kind === "clear_session"))
    assert.equal(ctx.running, false)
    assert.equal(ctx.messages[1].phase, "failed")
  }
})

test("busy is a notice: the running turn is untouched and the text comes back", () => {
  const start = { ...running(), pending: { text: "and by region?", mode: "auto" as const } }
  const { ctx, effects } = applyFrame(start, frame({ type: "error", code: "busy", content: "already running" }))

  assert.deepEqual(effects, [{ kind: "busy", draft: "and by region?" }])
  assert.equal(ctx.running, true)
  assert.equal(ctx.activeId, start.activeId)
  assert.equal(ctx.messages[1].phase, "routing")
  assert.equal(ctx.messages[1].error, undefined)
  assert.equal(ctx.pending, null)
})

test("busy with nothing pending gives back no draft", () => {
  const { effects } = applyFrame(running(), frame({ type: "error", code: "busy" }))
  assert.deepEqual(effects, [{ kind: "busy", draft: null }])
})

test("cancelled ends the turn and clears a pending message", () => {
  const start = { ...running(), pending: { text: "stop", mode: "auto" as const } }
  const { ctx } = applyFrame(start, frame({ type: "cancelled", reason: "user" }))
  assert.equal(ctx.messages[1].phase, "cancelled")
  assert.equal(ctx.running, false)
  assert.equal(ctx.activeId, null)
  assert.equal(ctx.pending, null)
})

test("a message sent as the turn ended is adopted as a new turn", () => {
  // The backend read it after the turn was over, so it ran as a fresh turn. The
  // UI had no placeholder for it; without adoption its reply was invisible.
  const start: TurnContext = { ...idle(), pending: { text: "thanks", mode: "fast" } }
  const { ctx } = applyFrame(start, frame({ type: "status", phase: "routing", content: "Understanding" }))

  assert.equal(ctx.messages.length, 2)
  assert.equal(ctx.messages[0].role, "user")
  assert.equal(ctx.messages[0].content, "thanks")
  assert.equal(ctx.messages[1].mode, "fast")
  assert.equal(ctx.running, true)
  assert.equal(ctx.activeId, ctx.messages[1].id)
  assert.equal(ctx.pending, null)
})

test("a stray frame with a pending message that is not the start of a turn is not adopted", () => {
  const start: TurnContext = { ...idle(), pending: { text: "thanks", mode: "auto" } }
  const { ctx } = applyFrame(start, frame({ type: "content_delta", content: "x" }))
  assert.equal(ctx.messages.length, 0)
  assert.equal(ctx.pending?.text, "thanks")
})

test("an artifact is handed to the side panel once and kept on the message", () => {
  const { ctx, effects } = applyFrame(running(), frame({ type: "artifact", kind: "script", name: "analysis.py" }))
  assert.equal(effects.filter((e) => e.kind === "artifact").length, 1)
  assert.equal(ctx.messages[1].artifacts.length, 1)
})

test("a plan gate ends the turn; a permission pause does not", () => {
  const gate = applyFrame(running(), frame({ type: "approval_required", tool: "execute_plan", plan: "1. do it" })).ctx
  assert.equal(gate.running, false)
  assert.equal(gate.activeId, null)
  assert.equal(gate.messages[1].phase, "awaiting_approval")

  const pause = applyFrame(running(), frame({ type: "approval_required", tool: "network", id: "p1" })).ctx
  assert.equal(pause.running, true)
  assert.ok(pause.activeId)
  assert.equal(pause.messages[1].phase, "awaiting_approval")
})

test("a conversational turn leaves no plan, steps or code on the message", () => {
  let ctx = running()
  for (const event of [
    { type: "status", phase: "routing" },
    { type: "route", workflow: "converse", intent: "conversation", plan: "none", needs_data: false },
    { type: "status", phase: "responding", content: "Replying" },
    { type: "content_delta", content: "Hello" },
    { type: "content_delta", content: " there" },
    { type: "final", response: "Hello there", route: { workflow: "converse" }, status: "completed" },
  ]) {
    ctx = applyFrame(ctx, frame(event)).ctx
  }
  const message = ctx.messages[1]
  assert.equal(message.content, "Hello there")
  assert.equal(message.route?.workflow, "converse")
  assert.equal(message.steps.length, 0)
  assert.equal(message.plan, undefined)
  assert.equal(message.trail.length, 0)
  assert.equal(ctx.running, false)
})

test("a second route frame after an escalation replaces the first", () => {
  let ctx = running()
  ctx = applyFrame(ctx, frame({ type: "route", workflow: "converse", source: "rules" })).ctx
  ctx = applyFrame(ctx, frame({ type: "route", workflow: "agentic", source: "escalation" })).ctx
  assert.equal(ctx.messages[1].route?.workflow, "agentic")
  assert.equal(ctx.messages[1].route?.source, "escalation")
})

test("branch-tagged frames never reach the main message", () => {
  const { ctx } = applyFrame(running(), frame({ type: "code", content: "print(1)", branch: "sub1" }))
  assert.equal(ctx.messages[1].code, undefined)
  assert.equal(ctx.messages[1].subagents["sub1"].code, "print(1)")
})
