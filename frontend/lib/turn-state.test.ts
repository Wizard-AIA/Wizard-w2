import { test } from "node:test"
import assert from "node:assert/strict"
import { reduceTurnState, type TurnState } from "./turn-state.ts"
import type { ChatMessage, ServerEvent } from "./types.ts"

const blankMessage = (): ChatMessage => ({
  id: "msg-1",
  role: "assistant", createdAt: Date.now(),
  content: "",
  steps: [],
  trail: [],
  artifacts: [],
  warnings: [],
  findings: [],
  assumptions: [],
  skillsUsed: [],
  downloads: [],
  subagents: {},
  streaming: true,
  phase: "routing",
})

test("state machine handles routing phase optimistic start", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  assert.equal(initial.globalPhase, "routing")
  const event: ServerEvent = { type: "status", phase: "planning", content: "Planning..." }
  const next = reduceTurnState(initial, event)
  assert.equal(next.globalPhase, "planning")
  assert.equal(next.message.phase, "planning")
  assert.equal(next.message.statusLabel, "Planning...")
})

test("state machine ignores stale frames after terminal frame", () => {
  const initial: TurnState = { message: { ...blankMessage(), streaming: false, phase: "done" }, isRunning: false, globalPhase: "idle" }
  const event: ServerEvent = { type: "content_delta", content: "stale" }
  const next = reduceTurnState(initial, event)
  assert.equal(next.message.content, "") // Stale frame ignored
})

test("state machine handles route", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  const event: ServerEvent = { type: "route", workflow: "converse" }
  const next = reduceTurnState(initial, event)
  assert.equal(next.message.route?.workflow, "converse")
})

test("state machine handles error code", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  const event: ServerEvent = { type: "error", code: "session_not_found", content: "Session not found." }
  const next = reduceTurnState(initial, event)
  assert.equal(next.message.error, "Session not found.")
  assert.equal(next.message.errorCode, "session_not_found")
  assert.equal(next.message.phase, "failed")
  assert.equal(next.isRunning, false)
  assert.equal(next.globalPhase, "idle")
})

test("state machine handles cancelled", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  const event: ServerEvent = { type: "cancelled", reason: "user" }
  const next = reduceTurnState(initial, event)
  assert.equal(next.message.phase, "cancelled")
  assert.equal(next.isRunning, false)
  assert.equal(next.globalPhase, "idle")
})

test("state machine handles plan gate approval_required", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  const event: ServerEvent = { type: "approval_required", tool: "execute_plan" }
  const next = reduceTurnState(initial, event)
  assert.equal(next.globalPhase, "awaiting_approval")
  assert.equal(next.message.phase, "awaiting_approval")
  assert.equal(next.isRunning, false) // Turn ended
})

test("state machine handles paused approval_required", () => {
  const initial: TurnState = { message: blankMessage(), isRunning: true, globalPhase: "routing" }
  const event: ServerEvent = { type: "approval_required", tool: "shell", id: "app-123" }
  const next = reduceTurnState(initial, event)
  assert.equal(next.globalPhase, "awaiting_approval")
  assert.equal(next.message.phase, "awaiting_approval")
  assert.equal(next.isRunning, true) // Mid-run pause
})
