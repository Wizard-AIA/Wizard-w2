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

import { clearStoredSessionId, storeSessionId, websocketUrl } from "./api"
import { recordUsageFrame } from "./usage-store"
import type { Artifact, ChatMessage, ServerEvent, Phase, AnalysisMode } from "./types"
import { reduceTurnState } from "./turn-state"

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



/**
 * Parses `final.analysis` -- the whole-turn snapshot mirroring
 * `AnalyticalState.to_dict()` -- into `AnalysisSnapshot`. Authoritative: this
 * replaces whatever `critic_finding`/`route_comparison`/`confidence` frames
 * built up live, since the backend's own lists are already cumulative for
 * the whole turn and merging would double every entry.
 */

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


/**
 * Folds the terminal frame's plain name list into the richer per-skill frames.
 *
 * The `skill` frames carry the layer and the match score; `final` carries names
 * only. Replacing one with the other would throw away whichever half arrived
 * second, so names already present keep their frame and the rest are added
 * with what is known about them.
 */

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
  const [busyAlert, setBusyAlert] = useState(false)

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
      patchActive((message) => {
        const state = { message, isRunning, globalPhase: phase }
        const next = reduceTurnState(state, event)
        if (next.globalPhase !== phase) setPhase(next.globalPhase)
        if (next.isRunning !== isRunning) setIsRunning(next.isRunning)
        if (!next.isRunning) activeIdRef.current = null
        if (event.type === "usage") recordUsageFrame(event as Record<string, unknown>)
        if (event.type === "artifact") artifactRef.current?.((event as unknown as Artifact))
        
        if (event.type === "error") {
            const code = (event.code as string) ?? undefined
            if (code === "busy") {
              setBusyAlert(true)
              setTimeout(() => setBusyAlert(false), 4000)
            }
            if (code === "session_not_found") {
               clearStoredSessionId()
            }
        }
        if (event.type === "session") {
            const id = String(event.session_id ?? "")
            if (id) {
              storeSessionId(id)
              sessionRef.current?.(id)
            }
        }

        return next.message
      })
    },
    [patchActive, isRunning, phase]
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

    socket.onclose = (event: WebSocketEventMap["close"]) => {
      if (!isCurrent()) return
      if (heartbeatRef.current) clearInterval(heartbeatRef.current)
      heartbeatRef.current = null
      socketRef.current = null
      setConnection("closed")

      if (event.code === 1008) {
        clearStoredSessionId()
      }

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
    cancel,
    busyAlert,
    clearSkillCandidate,
    clear,
    reconnect: connect,
  }
}
