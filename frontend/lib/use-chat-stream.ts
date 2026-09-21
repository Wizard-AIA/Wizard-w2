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
import type { AnalysisMode, Artifact, ChatMessage, Phase, ServerEvent } from "./types"
import { applyFrame } from "./turn-controller"
import { blankAssistant, blankUser } from "./turn-state"

const HEARTBEAT_MS = 25_000
const MAX_RECONNECT_DELAY_MS = 15_000

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
  /** Text the backend refused because a turn was running; the composer puts it back. */
  const [restoredDraft, setRestoredDraft] = useState<{ text: string; nonce: number } | null>(null)

  const socketRef = useRef<WebSocket | null>(null)
  const connectRef = useRef<(() => void) | null>(null)
  const activeIdRef = useRef<string | null>(null)
  // The socket's `onmessage` is assigned once, when the socket is created, so it
  // can only ever see what a ref holds *now*. Anything a frame handler decides
  // from (is a turn running, which message is live) therefore lives in a ref
  // that every writer updates in the same breath as the React state.
  const messagesRef = useRef<ChatMessage[]>([])
  const runningRef = useRef(false)
  const phaseRef = useRef<Phase>("idle")
  const busyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // A message sent while a turn was running. If the backend cancels the turn it
  // is consumed; if the backend refuses it, it goes back into the composer; if
  // the turn had just ended, the backend runs it as a new turn and it is adopted.
  const pendingInterruptRef = useRef<{ text: string; mode: AnalysisMode } | null>(null)
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

  /**
   * The only writer of the message list. The ref is updated synchronously, so a
   * frame handled right after another sees it; and no side effect ever runs
   * inside a React state updater, which StrictMode invokes twice.
   */
  const commitMessages = useCallback((update: (previous: ChatMessage[]) => ChatMessage[]) => {
    const next = update(messagesRef.current)
    messagesRef.current = next
    setMessages(next)
  }, [])

  const setRunning = useCallback((value: boolean) => {
    runningRef.current = value
    setIsRunning(value)
  }, [])

  const setPhaseNow = useCallback((value: Phase) => {
    phaseRef.current = value
    setPhase(value)
  }, [])

  /** Applies a mutation to the message currently being streamed. */
  const patchActive = useCallback(
    (mutate: (message: ChatMessage) => ChatMessage) => {
      const id = activeIdRef.current
      if (!id) return
      commitMessages((previous) => previous.map((message) => (message.id === id ? mutate(message) : message)))
    },
    [commitMessages],
  )

  const notifyBusy = useCallback((draft: string | null) => {
    setBusyAlert(true)
    if (busyTimerRef.current) clearTimeout(busyTimerRef.current)
    busyTimerRef.current = setTimeout(() => setBusyAlert(false), 5000)
    if (draft) setRestoredDraft({ text: draft, nonce: Date.now() })
  }, [])

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      const { ctx, effects } = applyFrame(
        {
          messages: messagesRef.current,
          activeId: activeIdRef.current,
          running: runningRef.current,
          phase: phaseRef.current,
          pending: pendingInterruptRef.current,
        },
        event,
      )

      if (ctx.messages !== messagesRef.current) commitMessages(() => ctx.messages)
      if (ctx.running !== runningRef.current) setRunning(ctx.running)
      if (ctx.phase !== phaseRef.current) setPhaseNow(ctx.phase)
      activeIdRef.current = ctx.activeId
      pendingInterruptRef.current = ctx.pending

      // Each effect runs here, once, outside any React updater.
      for (const effect of effects) {
        switch (effect.kind) {
          case "session":
            storeSessionId(effect.id)
            sessionRef.current?.(effect.id)
            break
          case "usage":
            recordUsageFrame(effect.event as Record<string, unknown>)
            break
          case "artifact":
            artifactRef.current?.(effect.artifact)
            break
          case "clear_session":
            clearStoredSessionId()
            break
          case "busy":
            notifyBusy(effect.draft)
            break
        }
      }
    },
    [commitMessages, notifyBusy, setPhaseNow, setRunning],
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
        setRunning(false)
        setPhaseNow("idle")
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
  }, [handleEvent, patchActive, setPhaseNow, setRunning])

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
        commitMessages((previous) => [
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
    [commitMessages, connect],
  )

  const sendMessage = useCallback(
    (content: string, mode: AnalysisMode) => {
      const trimmed = content.trim()
      if (!trimmed || isRunning) return

      const userMessage = blankUser(trimmed)
      const assistant = { ...blankAssistant(), mode, instruction: trimmed }
      activeIdRef.current = assistant.id

      commitMessages((previous) => [...previous, userMessage, assistant])
      setRunning(true)
      setPhaseNow("routing")

      if (!send({ type: "message", content: trimmed, mode })) {
        setRunning(false)
        activeIdRef.current = null
      }
    },
    [commitMessages, isRunning, send, setPhaseNow, setRunning],
  )

  /**
   * Sends a message while a turn is running.
   *
   * The backend decides what it is: a request to stop cancels the turn, and
   * anything else is refused as busy and handed back so the text is not lost. The
   * running message is never touched here, so its frames keep landing on it.
   * With nothing running it is simply a message.
   */
  const interrupt = useCallback(
    (content: string, mode: AnalysisMode) => {
      const trimmed = content.trim()
      if (!trimmed) return
      if (!runningRef.current) {
        sendMessage(trimmed, mode)
        return
      }
      pendingInterruptRef.current = { text: trimmed, mode }
      if (!send({ type: "message", content: trimmed, mode })) pendingInterruptRef.current = null
    },
    [send, sendMessage],
  )

  const respondToApproval = useCallback(
    (message: ChatMessage, approved: boolean) => {
      const approval = message.approval
      if (!approval) return

      commitMessages((previous) =>
        previous.map((item) => (item.id === message.id ? { ...item, approval: null } : item)),
      )

      // A permission gate is answered in place: the run never stopped, so this
      // frame is a reply, not the start of anything. Rebuilding the turn here
      // would throw away the investigation the paused run is still holding.
      // The run resumes either way — a decline is something it routes around,
      // not something that ends it — so the phase moves on regardless.
      if (approval.id) {
        send({ type: "approval", approved, id: approval.id })
        setPhaseNow("generating")
        return
      }

      if (!approved) {
        commitMessages((previous) =>
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
      commitMessages((previous) => [...previous, assistant])
      setRunning(true)
      setPhaseNow("routing")

      send({
        type: "approval",
        approved: true,
        tool: approval.tool,
        content: instruction,
        plan: approval.plan,
        query: approval.query,
      })
    },
    [commitMessages, messages, send, setPhaseNow, setRunning],
  )

  const cancel = useCallback(() => {
    send({ type: "cancel" })
    // Settled here as well as by the backend's `cancelled` frame: the socket may
    // already be gone, and the user must never wait on a reply to be able to type.
    patchActive((message) => ({ ...message, streaming: false, phase: "cancelled" }))
    pendingInterruptRef.current = null
    setRunning(false)
    setPhaseNow("idle")
    activeIdRef.current = null
  }, [patchActive, send, setPhaseNow, setRunning])

  const clear = useCallback(() => {
    commitMessages(() => [])
    setPhaseNow("idle")
    setRunning(false)
    activeIdRef.current = null
    pendingInterruptRef.current = null
  }, [commitMessages, setPhaseNow, setRunning])

  /**
   * Takes the promotion offer off a message once it has been acted on.
   *
   * Local only — whether it was promoted or dismissed is recorded server-side by
   * the call the card already made, and this just stops the card rendering.
   */
  const clearSkillCandidate = useCallback(
    (messageId: string) => {
      commitMessages((previous) =>
        previous.map((item) => (item.id === messageId ? { ...item, skillCandidate: null } : item)),
      )
    },
    [commitMessages],
  )

  useEffect(
    () => () => {
      if (busyTimerRef.current) clearTimeout(busyTimerRef.current)
    },
    [],
  )

  return {
    messages,
    connection,
    isRunning,
    phase,
    sendMessage,
    interrupt,
    respondToApproval,
    cancel,
    busyAlert,
    restoredDraft,
    clearSkillCandidate,
    clear,
    reconnect: connect,
  }
}
