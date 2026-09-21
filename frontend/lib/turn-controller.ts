/**
 * Decides what one socket frame does to the conversation.
 *
 * `reduceTurnState` folds a frame into one message. This sits above it and owns
 * the questions that come before that: does the frame belong to a turn at all,
 * which message is the live one, was it a message sent while a turn was running,
 * and what has to happen outside the message list (store the session id, record
 * usage, hand an artifact to the side panel).
 *
 * It is a pure function so those decisions can be tested. The hook applies the
 * returned context and runs each effect exactly once. Two bugs a version of this
 * code had are the reason it is separate: a frame handler that read `isRunning`
 * from a stale closure (so a finished turn never stopped "running"), and one that
 * only handled frames while a turn was active (so the `session` frame, which
 * arrives before any turn exists, was dropped).
 */

// The `.ts` extension is what lets Node's test runner load this file directly.
import { artifactFromEvent, blankAssistant, blankUser, reduceTurnState } from "./turn-state.ts"
import type { AnalysisMode, Artifact, ChatMessage, Phase, ServerEvent } from "./types"

export interface PendingInterrupt {
  text: string
  mode: AnalysisMode
}

export interface TurnContext {
  messages: ChatMessage[]
  /** The message the running turn is writing into, if a turn is running. */
  activeId: string | null
  running: boolean
  phase: Phase
  /** A message sent while a turn was running, not yet resolved by the backend. */
  pending: PendingInterrupt | null
}

export type FrameEffect =
  | { kind: "session"; id: string }
  | { kind: "usage"; event: ServerEvent }
  | { kind: "artifact"; artifact: Artifact }
  | { kind: "clear_session" }
  /** The backend refused a message because a turn is running. `draft` is what to give back. */
  | { kind: "busy"; draft: string | null }

export interface FrameResult {
  ctx: TurnContext
  effects: FrameEffect[]
}

function isSessionGone(event: ServerEvent): boolean {
  const code = typeof event.code === "string" ? event.code : undefined
  const text = String(event.content ?? "")
  return code === "session_not_found" || text.includes("Session not found") || text.includes("expired")
}

export function applyFrame(ctx: TurnContext, event: ServerEvent): FrameResult {
  const effects: FrameEffect[] = []

  // Frames about the connection, not about a turn: handled whether or not one runs.
  if (event.type === "session") {
    const id = String(event.session_id ?? "")
    return { ctx, effects: id ? [{ kind: "session", id }] : [] }
  }
  if (event.type === "usage") return { ctx, effects: [{ kind: "usage", event }] }
  if ((event.type as string) === "pong") return { ctx, effects }

  const code = typeof event.code === "string" ? event.code : undefined
  if (event.type === "error") {
    if (isSessionGone(event)) effects.push({ kind: "clear_session" })
    // This message was refused; the turn that is running is fine. A notice and the
    // text back, never a failed turn.
    if (code === "busy") {
      effects.push({ kind: "busy", draft: ctx.pending?.text ?? null })
      return { ctx: { ...ctx, pending: null }, effects }
    }
  }

  let messages = ctx.messages
  let activeId = ctx.activeId
  let running = ctx.running
  let phase = ctx.phase
  let pending = ctx.pending

  if (!activeId) {
    if (pending && (event.type === "status" || event.type === "route")) {
      // The turn had already ended when the backend read the message, so it ran
      // as a new turn. Show it as one.
      const assistant: ChatMessage = { ...blankAssistant(), mode: pending.mode, instruction: pending.text }
      messages = [...messages, blankUser(pending.text), assistant]
      activeId = assistant.id
      running = true
      phase = "routing"
      pending = null
    } else {
      if (event.type === "error") {
        // Nothing is running, but the user should still see that something failed.
        const failed: ChatMessage = {
          ...blankAssistant(),
          streaming: false,
          error: String(event.content ?? "Something went wrong."),
          errorCode: code,
          phase: "failed",
        }
        return { ctx: { ...ctx, messages: [...messages, failed], running: false, phase: "idle" }, effects }
      }
      // A stale frame for a turn that is already over.
      return { ctx, effects }
    }
  }

  const current = messages.find((message) => message.id === activeId)
  if (!current) return { ctx, effects }

  const next = reduceTurnState({ message: current, isRunning: running, globalPhase: phase }, event)
  if (event.type === "artifact") effects.push({ kind: "artifact", artifact: artifactFromEvent(event) })
  if (event.type === "cancelled") pending = null

  return {
    ctx: {
      messages: messages.map((message) => (message.id === activeId ? next.message : message)),
      activeId: next.isRunning ? activeId : null,
      running: next.isRunning,
      phase: next.globalPhase,
      pending,
    },
    effects,
  }
}

/**
 * A plan waiting for approval belongs to the turn before whatever is sent next.
 *
 * The backend supersedes it: typing "go ahead" runs that plan, and any other
 * message replaces it. Either way the earlier message's Approve box is stale, and
 * left in place it would sit in the history as an action that no longer does
 * anything. A permission prompt (it has an `id`) belongs to a running turn and is
 * never touched here.
 */
export function settlePlanGates(messages: ChatMessage[]): ChatMessage[] {
  if (!messages.some((message) => message.approval && !message.approval.id)) return messages
  return messages.map((message) =>
    message.approval && !message.approval.id ? { ...message, approval: null } : message,
  )
}
