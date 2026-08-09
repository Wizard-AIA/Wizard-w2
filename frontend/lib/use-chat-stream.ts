"use client"

/**
 * WebSocket chat with genuine token streaming.
 *
 * The previous implementation opened a socket per message, waited for the
 * finished response, then faked a stream by revealing four words every 30ms.
 * This keeps one connection open (with reconnect + heartbeat) and appends each
 * `*_delta` frame to the live message as it arrives, so what you see is the
 * model's actual output rate.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import { storeSessionId, websocketUrl } from "./api"
import { recordUsageFrame } from "./usage-store"
import type {
  ActionKind,
  AnalysisMode,
  ApprovalRequest,
  Artifact,
  ChatMessage,
  Grounding,
  Phase,
  RunStep,
  ServerEvent,
  SkillCandidate,
  SkillUse,
  SubagentBranch,
  TrailEntry,
  Verification,
} from "./types"

const HEARTBEAT_MS = 25_000
const MAX_RECONNECT_DELAY_MS = 15_000

function newId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`
}

/**
 * Closes a socket the hook no longer owns.
 *
 * `close()` on a socket that is still CONNECTING aborts the handshake and the
 * browser logs "WebSocket is closed before the connection is established" at
 * error level. React StrictMode remounts every effect in development, so the
 * mount/cleanup/mount cycle hit that on every single page load. Letting the
 * handshake finish and closing on open is the same outcome without the noise,
 * and — more importantly — it closes deterministically rather than leaving the
 * server holding a connection whose client has already walked away.
 */
function retireSocket(socket: WebSocket | null): void {
  if (!socket) return
  socket.onmessage = null
  socket.onerror = null
  socket.onclose = null
  if (socket.readyState === WebSocket.CONNECTING) {
    socket.onopen = () => socket.close()
    return
  }
  socket.onopen = null
  socket.close()
}

function blankAssistant(): ChatMessage {
  return {
    id: newId(),
    role: "assistant",
    content: "",
    createdAt: Date.now(),
    steps: [],
    artifacts: [],
    warnings: [],
    downloads: [],
    trail: [],
    findings: [],
    assumptions: [],
    skillsUsed: [],
    subagents: {},
    streaming: true,
    phase: "planning",
  }
}

function blankUser(content: string): ChatMessage {
  return {
    id: newId(),
    role: "user",
    content,
    createdAt: Date.now(),
    steps: [],
    artifacts: [],
    warnings: [],
    downloads: [],
    trail: [],
    findings: [],
    assumptions: [],
    skillsUsed: [],
    subagents: {},
  }
}

/**
 * Routes one branch-tagged frame into its own `SubagentBranch`, rather than
 * into the top-level fields the same event type would otherwise patch.
 *
 * A subagent reuses the main loop's own handlers unmodified, so it emits the
 * same event types (`action`, `observation`, `status`, `code`, `stdout`,
 * `iteration_start`) — only tagged with `branch` in the raw frame. Without
 * this, a subagent's own status line would overwrite the main thread's, and
 * two concurrent branches' `action`/`observation` frames would race on "close
 * the most recent open entry", which is only correct under strict seriality.
 * Each branch's own sequence *is* strictly serial (one loop, one task), so the
 * same matching rule the top-level trail uses is safe here, just scoped per
 * branch instead of per message.
 */
function applyBranchEvent(message: ChatMessage, event: ServerEvent, branch: string): ChatMessage {
  const existing = message.subagents[branch]
  const group = String(event.group ?? existing?.group ?? "")
  const current: SubagentBranch = existing ?? { id: branch, goal: "", group, trail: [], done: false }
  let next: SubagentBranch = current

  switch (event.type) {
    case "subagent_start":
      next = { ...current, goal: String(event.goal ?? ""), group }
      break

    case "subagent_end":
      next = {
        ...current,
        done: true,
        ok: Boolean(event.ok),
        costUsd: (event.cost_usd as number | null | undefined) ?? null,
        totalTokens: Number(event.total_tokens ?? 0),
        calls: Number(event.calls ?? 0),
      }
      break

    case "iteration_start":
      next = { ...current, iteration: Number(event.n ?? 0), iterationBudget: Number(event.budget ?? 0) }
      break

    case "action": {
      const entry: TrailEntry = {
        id: newId(),
        iteration: current.iteration ?? current.trail.length + 1,
        kind: (event.kind as ActionKind) ?? "code",
        goal: String(event.goal ?? ""),
        rationale: (event.rationale as string) || undefined,
        inferred: Boolean(event.inferred),
      }
      next = { ...current, trail: [...current.trail, entry] }
      break
    }

    case "observation": {
      const trail = [...current.trail]
      for (let index = trail.length - 1; index >= 0; index -= 1) {
        if (trail[index].observation === undefined) {
          trail[index] = {
            ...trail[index],
            observation: String(event.summary ?? ""),
            ok: Boolean(event.ok),
            truncated: Boolean(event.truncated),
            chars: Number(event.chars ?? 0),
          }
          break
        }
      }
      next = { ...current, trail }
      break
    }

    case "status":
      next = { ...current, statusLabel: String(event.content ?? ""), phase: (event.phase as Phase) ?? current.phase }
      break

    case "code":
      next = { ...current, code: String(event.content ?? "") }
      break

    case "stdout":
      next = { ...current, stdout: (current.stdout ?? "") + String(event.content ?? "") }
      break

    default:
      // step_start/step_end/assumption/etc. are not surfaced per branch --
      // findings and assumptions still reach the top-level lists once the
      // branch folds back into the parent's own investigation, and a
      // branch's fine-grained code-writing/execution steps are not needed
      // for the panel this renders.
      break
  }

  return { ...message, subagents: { ...message.subagents, [branch]: next } }
}

/**
 * Folds the terminal frame's plain name list into the richer per-skill frames.
 *
 * The `skill` frames carry the layer and the match score; `final` carries names
 * only. Replacing one with the other would throw away whichever half arrived
 * second, so names already present keep their frame and the rest are added
 * with what is known about them.
 */
function mergeSkills(existing: SkillUse[], names: string[]): SkillUse[] {
  const seen = new Set(existing.map((skill) => skill.name))
  const extra = names
    .filter((name) => name && !seen.has(name))
    .map((name): SkillUse => ({ name, layer: "user" }))
  return extra.length ? [...existing, ...extra] : existing
}

export type ConnectionState = "connecting" | "open" | "closed" | "error"

interface UseChatStreamOptions {
  onArtifact?: (artifact: Artifact) => void
  onSessionId?: (id: string) => void
}

export function useChatStream({ onArtifact, onSessionId }: UseChatStreamOptions = {}) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [connection, setConnection] = useState<ConnectionState>("connecting")
  const [isRunning, setIsRunning] = useState(false)
  const [phase, setPhase] = useState<Phase>("idle")

  const socketRef = useRef<WebSocket | null>(null)
  const connectRef = useRef<(() => void) | null>(null)
  const activeIdRef = useRef<string | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const attemptsRef = useRef(0)
  const shouldReconnectRef = useRef(true)

  // Callbacks are held in refs so the socket effect does not resubscribe when a
  // parent re-renders with new closures.
  const artifactRef = useRef(onArtifact)
  const sessionRef = useRef(onSessionId)
  useEffect(() => {
    artifactRef.current = onArtifact
    sessionRef.current = onSessionId
  }, [onArtifact, onSessionId])

  /** Applies a mutation to the message currently being streamed. */
  const patchActive = useCallback((mutate: (message: ChatMessage) => ChatMessage) => {
    const id = activeIdRef.current
    if (!id) return
    setMessages((previous) =>
      previous.map((message) => (message.id === id ? mutate(message) : message)),
    )
  }, [])

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      // A branch-tagged frame never reaches the switch below: it would
      // otherwise patch the top-level fields the same event type patches for
      // the main thread (overwriting `code`/`stdout`/`phase` with a
      // subagent's own, or racing another concurrent branch's `action`/
      // `observation` pairing). See `applyBranchEvent`.
      const branch = typeof event.branch === "string" ? event.branch : ""
      if (branch) {
        patchActive((message) => applyBranchEvent(message, event, branch))
        return
      }

      switch (event.type) {
        case "session": {
          const id = String(event.session_id ?? "")
          if (id) {
            storeSessionId(id)
            sessionRef.current?.(id)
          }
          break
        }

        case "status": {
          const nextPhase = (event.phase as Phase) ?? "idle"
          setPhase(nextPhase)
          patchActive((message) => ({
            ...message,
            phase: nextPhase,
            statusLabel: String(event.content ?? ""),
          }) as ChatMessage)
          break
        }

        case "step_start": {
          const step: RunStep = {
            id: String(event.id),
            label: String(event.label ?? ""),
            kind: (event.kind as RunStep["kind"]) ?? "plan",
            status: "running",
          }
          patchActive((message) => ({ ...message, steps: [...message.steps, step] }))
          break
        }

        case "step_end": {
          patchActive((message) => ({
            ...message,
            steps: message.steps.map((step) =>
              step.id === String(event.id)
                ? {
                    ...step,
                    status: event.ok ? "done" : "failed",
                    durationMs: Number(event.duration_ms ?? 0),
                  }
                : step,
            ),
          }))
          break
        }

        case "reasoning_delta":
          patchActive((message) => ({
            ...message,
            reasoning: (message.reasoning ?? "") + String(event.content ?? ""),
          }))
          break

        case "plan_delta":
          patchActive((message) => ({
            ...message,
            plan: (message.plan ?? "") + String(event.content ?? ""),
          }))
          break

        case "content_delta":
          patchActive((message) => ({
            ...message,
            content: message.content + String(event.content ?? ""),
          }))
          break

        case "code":
          patchActive((message) => ({ ...message, code: String(event.content ?? "") }))
          break

        case "stdout":
          patchActive((message) => ({
            ...message,
            stdout: (message.stdout ?? "") + String(event.content ?? ""),
          }))
          break

        case "artifact": {
          const artifact: Artifact = {
            kind: event.kind as Artifact["kind"],
            name: event.name as string | undefined,
            data: event.data as string | undefined,
            text: event.text as string | undefined,
          }
          patchActive((message) => ({ ...message, artifacts: [...message.artifacts, artifact] }))
          artifactRef.current?.(artifact)
          break
        }

        case "warning":
          patchActive((message) => ({
            ...message,
            warnings: [...message.warnings, String(event.content ?? "")],
          }))
          break

        // Session totals, not a delta. Pushed into the shared store so the cost
        // readout in the rail moves when a turn ends rather than on next load.
        // Only sent when a cloud model ran, so a local-only session never sees it.
        case "usage":
          recordUsageFrame(event as Record<string, unknown>)
          break

        case "iteration_start":
          patchActive((message) => ({
            ...message,
            iteration: Number(event.n ?? 0),
            iterationBudget: Number(event.budget ?? 0),
            mode: (event.mode as AnalysisMode) ?? message.mode,
          }))
          break

        case "action": {
          // Opens a trail entry. Its observation arrives in a separate frame,
          // so the entry is rendered as in-flight until then.
          const entry: TrailEntry = {
            id: newId(),
            iteration: 0,
            kind: (event.kind as ActionKind) ?? "code",
            goal: String(event.goal ?? ""),
            rationale: (event.rationale as string) || undefined,
            inferred: Boolean(event.inferred),
            // Not set here: on a `parallel` entry the group id is only known
            // once `_act_parallel` actually runs, which is after this frame
            // fires. It arrives on the matching `observation` frame instead.
          }
          patchActive((message) => ({
            ...message,
            trail: [...message.trail, { ...entry, iteration: message.iteration ?? message.trail.length + 1 }],
          }))
          break
        }

        case "observation": {
          // Closes the most recent open entry. Matching on "last without an
          // observation" rather than an id keeps the protocol one-way: the
          // backend never has to correlate the two frames.
          patchActive((message) => {
            const trail = [...message.trail]
            for (let index = trail.length - 1; index >= 0; index -= 1) {
              if (trail[index].observation === undefined) {
                trail[index] = {
                  ...trail[index],
                  observation: String(event.summary ?? ""),
                  ok: Boolean(event.ok),
                  truncated: Boolean(event.truncated),
                  chars: Number(event.chars ?? 0),
                  // A `parallel` entry's group is only known once the action
                  // has actually run (the `action` frame fires before
                  // `_act_parallel` computes one), so it arrives here instead.
                  group: (event.group as string) || trail[index].group,
                }
                break
              }
            }
            return { ...message, trail }
          })
          break
        }

        case "finding":
          patchActive((message) => ({
            ...message,
            findings: [...message.findings, String(event.text ?? "")],
          }))
          break

        case "assumption": {
          const text = String(event.text ?? "")
          patchActive((message) =>
            // The backend re-emits the full ledger at the end, so dedupe here
            // rather than showing every caveat twice.
            message.assumptions.includes(text)
              ? message
              : { ...message, assumptions: [...message.assumptions, text] },
          )
          break
        }

        case "plan_revised": {
          const plan = String(event.plan ?? "")
          const why = String(event.why ?? "")
          patchActive((message) => ({
            ...message,
            plan,
            findings: why && !message.findings.includes(why) ? [...message.findings, why] : message.findings,
          }))
          break
        }

        case "skill": {
          // Deduped by name: a skill can be matched at planning and again by a
          // `consult`, and "informed by X, X" says nothing extra.
          const use: SkillUse = {
            name: String(event.name ?? ""),
            description: event.description as string | undefined,
            layer: (event.layer as SkillUse["layer"]) ?? "user",
            score: typeof event.score === "number" ? event.score : undefined,
            phase: event.phase as string | undefined,
          }
          patchActive((message) =>
            message.skillsUsed.some((existing) => existing.name === use.name)
              ? message
              : { ...message, skillsUsed: [...message.skillsUsed, use] },
          )
          break
        }

        case "skill_candidate":
          patchActive((message) => ({
            ...message,
            skillCandidate: {
              id: Number(event.id ?? 0),
              kind: (event.kind as SkillCandidate["kind"]) ?? "recurring",
              label: String(event.label ?? ""),
              instruction: String(event.instruction ?? ""),
              occurrences: Number(event.occurrences ?? 0),
              threshold: Number(event.threshold ?? 0),
              suggested_name: String(event.suggested_name ?? ""),
              plan: event.plan as string | undefined,
              code: event.code as string | undefined,
            },
          }))
          break

        case "verification": {
          const verification: Verification = {
            status: (event.status as Verification["status"]) ?? "inconclusive",
            detail: String(event.detail ?? ""),
          }
          patchActive((message) => ({ ...message, verification }))
          break
        }

        case "approval_required": {
          const approval: ApprovalRequest = {
            tool: (event.tool as string) ?? "execute_plan",
            prompt: String(event.prompt ?? "Confirm to continue."),
            plan: event.plan as string | undefined,
            query: event.query as string | undefined,
            id: event.id as string | undefined,
            category: event.category as string | undefined,
            subject: event.subject as string | undefined,
            detail: event.detail as string | undefined,
          }
          patchActive((message) => ({
            ...message,
            approval,
            plan: (event.plan as string) ?? message.plan,
            streaming: false,
            phase: "awaiting_approval",
          }))
          setPhase("awaiting_approval")
          // An `id` means the turn is *paused*, not finished: it is still on the
          // server holding its investigation state, waiting for this answer. The
          // plan gate has no id because it really did end its turn, and a new one
          // has to be started to resume it.
          if (!approval.id) {
            setIsRunning(false)
            activeIdRef.current = null
          }
          break
        }

        case "error": {
          const text = String(event.content ?? "Something went wrong.")
          if (activeIdRef.current) {
            patchActive((message) => ({
              ...message,
              error: text,
              streaming: false,
              phase: "failed",
            }))
          } else {
            setMessages((previous) => [
              ...previous,
              { ...blankAssistant(), streaming: false, error: text, phase: "failed" },
            ])
          }
          setIsRunning(false)
          setPhase("idle")
          activeIdRef.current = null
          break
        }

        case "final": {
          const finalText = String(event.response ?? "")
          patchActive((message) => ({
            ...message,
            // The streamed content is authoritative; fall back only if the
            // answer never streamed (e.g. the model returned in one chunk).
            content: message.content || finalText,
            code: (event.code as string) || message.code,
            downloads: (event.downloads as string[]) ?? [],
            // The run emits warnings as they happen and again in the terminal
            // frame; without deduping, every one is shown twice.
            warnings: Array.from(
              new Set([...message.warnings, ...(((event.warnings as string[]) ?? []) || [])]),
            ),
            findings: Array.from(
              new Set([...message.findings, ...(((event.findings as string[]) ?? []) || [])]),
            ),
            assumptions: Array.from(
              new Set([...message.assumptions, ...(((event.assumptions as string[]) ?? []) || [])]),
            ),
            grounding: (event.grounding as Grounding) ?? message.grounding,
            // Reconciled against the per-skill frames rather than replacing
            // them: the frames carry the layer and the score, and this list is
            // only names. Anything named here that never arrived as a frame is
            // added with what is known.
            skillsUsed: mergeSkills(message.skillsUsed, (event.skills_used as string[]) ?? []),
            iteration: Number(event.iterations ?? message.iteration ?? 0),
            tier: (event.tier as string) ?? message.tier,
            elapsedMs: Number(event.elapsed_ms ?? 0),
            messageId: (event.message_id as number | null | undefined) ?? message.messageId ?? null,
            streaming: false,
            phase: "done",
          }))
          setIsRunning(false)
          setPhase("idle")
          activeIdRef.current = null
          break
        }

        default:
          break
      }
    },
    [patchActive],
  )

  const connect = useCallback(() => {
    if (typeof window === "undefined") return
    // CONNECTING counts as connected. Testing only for OPEN meant a `send`
    // during the handshake opened a second socket to the same session.
    const existing = socketRef.current?.readyState
    if (existing === WebSocket.OPEN || existing === WebSocket.CONNECTING) return

    // State is deliberately not set here. `connect` is called from an effect on
    // mount, and a synchronous setState in an effect body triggers a cascading
    // render. "connecting" is the initial value, and every later transition is
    // driven by the socket's own lifecycle handlers below.
    let socket: WebSocket
    try {
      socket = new WebSocket(websocketUrl())
    } catch {
      // Construction only throws on a malformed URL. Report it asynchronously so
      // `connect` contains no synchronous setState at all.
      queueMicrotask(() => setConnection("error"))
      return
    }
    socketRef.current = socket

    // Every handler below checks it is still the socket the hook holds. Without
    // that, a socket discarded by a remount or a reconnect keeps acting as the
    // live one: its `onclose` nulls `socketRef` out from under its replacement
    // and schedules yet another connect, so the replacement is orphaned while
    // still open. The server counts that orphan against
    // `WS_MAX_CONCURRENT_PER_IP` (4), which is why one tab was costing two
    // connections and two tabs exhausted the limit.
    const isCurrent = () => socketRef.current === socket

    socket.onopen = () => {
      if (!isCurrent()) {
        socket.close()
        return
      }
      attemptsRef.current = 0
      setConnection("open")
      heartbeatRef.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }))
        }
      }, HEARTBEAT_MS)
    }

    socket.onmessage = (raw) => {
      if (!isCurrent()) return
      try {
        handleEvent(JSON.parse(raw.data) as ServerEvent)
      } catch {
        // A malformed frame must not tear down the stream.
      }
    }

    socket.onerror = () => {
      if (isCurrent()) setConnection("error")
    }

    socket.onclose = () => {
      if (!isCurrent()) return
      if (heartbeatRef.current) clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
      socketRef.current = null
      setConnection("closed")

      // A close mid-run would otherwise leave the UI spinning forever.
      if (activeIdRef.current) {
        patchActive((message) => ({
          ...message,
          streaming: false,
          error: message.content ? undefined : "The connection dropped before the answer completed.",
        }))
        activeIdRef.current = null
        setIsRunning(false)
        setPhase("idle")
      }

      if (shouldReconnectRef.current) {
        attemptsRef.current += 1
        const cappedDelay = Math.min(1000 * 2 ** (attemptsRef.current - 1), MAX_RECONNECT_DELAY_MS)
        // Full jitter: every tab reconnecting off the same fixed schedule
        // after a backend restart is a thundering herd hitting the server at
        // once. A random delay in [0, cappedDelay] spreads that out.
        const delay = Math.floor(Math.random() * cappedDelay)
        // Reached through a ref so the callback does not have to close over
        // itself, which would make it its own dependency.
        reconnectRef.current = setTimeout(() => {
          setConnection("connecting")
          connectRef.current?.()
        }, delay)
      }
    }
  }, [handleEvent, patchActive])

  // Keeps the reconnect timer pointing at the current `connect` closure.
  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  useEffect(() => {
    shouldReconnectRef.current = true
    connect()
    return () => {
      shouldReconnectRef.current = false
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      if (heartbeatRef.current) clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
      const outgoing = socketRef.current
      // Cleared first: the handlers are keyed on identity, so the outgoing
      // socket must stop being "current" before it is retired.
      socketRef.current = null
      retireSocket(outgoing)
    }
  }, [connect])

  const send = useCallback(
    (payload: Record<string, unknown>) => {
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        setMessages((previous) => [
          ...previous,
          {
            ...blankAssistant(),
            streaming: false,
            error: "Not connected to the analysis server. Retrying automatically…",
            phase: "failed",
          },
        ])
        connect()
        return false
      }
      socket.send(JSON.stringify(payload))
      return true
    },
    [connect],
  )

  const sendMessage = useCallback(
    (content: string, mode: AnalysisMode) => {
      const trimmed = content.trim()
      if (!trimmed || isRunning) return

      const userMessage = blankUser(trimmed)
      const assistant = { ...blankAssistant(), mode, instruction: trimmed }
      activeIdRef.current = assistant.id

      setMessages((previous) => [...previous, userMessage, assistant])
      setIsRunning(true)
      setPhase("planning")

      if (!send({ type: "message", content: trimmed, mode })) {
        setIsRunning(false)
        activeIdRef.current = null
      }
    },
    [isRunning, send],
  )

  const respondToApproval = useCallback(
    (message: ChatMessage, approved: boolean) => {
      const approval = message.approval
      if (!approval) return

      setMessages((previous) =>
        previous.map((item) => (item.id === message.id ? { ...item, approval: null } : item)),
      )

      // A permission gate is answered in place: the run never stopped, so this
      // frame is a reply, not the start of anything. Rebuilding the turn here
      // would throw away the investigation the paused run is still holding.
      // The run resumes either way — a decline is something it routes around,
      // not something that ends it — so the phase moves on regardless.
      if (approval.id) {
        send({ type: "approval", approved, id: approval.id })
        setPhase("generating")
        return
      }

      if (!approved) {
        setMessages((previous) =>
          previous.map((item) =>
            item.id === message.id
              ? { ...item, content: item.content || "Plan rejected.", phase: "done" }
              : item,
          ),
        )
        return
      }

      // Find the user turn this approval belongs to so the instruction survives.
      const index = messages.findIndex((item) => item.id === message.id)
      const instruction =
        [...messages.slice(0, index)].reverse().find((item) => item.role === "user")?.content ?? ""

      const assistant = { ...blankAssistant(), instruction }
      activeIdRef.current = assistant.id
      setMessages((previous) => [...previous, assistant])
      setIsRunning(true)
      setPhase("generating")

      send({
        type: "approval",
        approved: true,
        tool: approval.tool,
        content: instruction,
        plan: approval.plan,
        query: approval.query,
      })
    },
    [messages, send],
  )

  const cancel = useCallback(() => {
    send({ type: "cancel" })
    setIsRunning(false)
    setPhase("idle")
    patchActive((message) => ({ ...message, streaming: false, phase: "idle" }))
    activeIdRef.current = null
  }, [patchActive, send])

  const clear = useCallback(() => {
    setMessages([])
    setPhase("idle")
    setIsRunning(false)
    activeIdRef.current = null
  }, [])

  /**
   * Takes the promotion offer off a message once it has been acted on.
   *
   * Local only — whether it was promoted or dismissed is recorded server-side by
   * the call the card already made, and this just stops the card rendering.
   */
  const clearSkillCandidate = useCallback((messageId: string) => {
    setMessages((previous) =>
      previous.map((item) => (item.id === messageId ? { ...item, skillCandidate: null } : item)),
    )
  }, [])

  return {
    messages,
    connection,
    isRunning,
    phase,
    sendMessage,
    respondToApproval,
    clearSkillCandidate,
    cancel,
    clear,
    reconnect: connect,
  }
}
