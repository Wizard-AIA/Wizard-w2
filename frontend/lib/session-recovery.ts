// What the client does when the backend no longer knows its session id.
//
// Sessions live in the backend's memory, so every backend restart (an upgrade,
// `wizard stop && wizard start`, a crash) invalidates the id a still-open tab
// has in localStorage. The backend answers such an id with 404 on purpose
// (issue #94: silently minting a replacement would detach the caller from the
// data mode and permissions it thought it had), so recovery belongs here.
//
// Kept free of `@/` imports and of anything React or DOM so it can be tested
// with `node --test` (see session-recovery.test.ts). Only erasable TypeScript
// syntax, for the same reason.

export interface SessionStore {
  get(): string | null
  set(id: string): void
  clear(): void
}

/**
 * True when a response says the session id we sent is gone.
 *
 * A bare 404 is not enough: `GET /api/workspace/file/x` also answers 404 for a
 * missing file, and that must not throw away a perfectly good session. The
 * prefix matches backend/src/api/deps.py (`require_session`); a backend test
 * pins it so the two cannot drift apart unnoticed.
 */
export function isSessionGone(status: number, error: { message: string, code?: string }): boolean {
  return status === 404 && (error.code === "session_not_found" || error.message.startsWith("Session not found"))
}

/** Only reads are replayed: re-sending a write into a brand-new, empty session would hide that the user's data is gone. */
export function isReplayable(method: string | undefined): boolean {
  const verb = (method ?? "GET").toUpperCase()
  return verb === "GET" || verb === "HEAD"
}

/**
 * Builds the single-flight recovery step.
 *
 * `recover(deadId)` resolves to a live session id. Every request that fails at
 * the same moment (the app fires four on load) shares one `mint()` call rather
 * than each creating a session of its own, and an id that something else has
 * already replaced, such as the chat socket reconnecting, is reused instead of
 * being cleared and minted over.
 */
export function createSessionRecovery(store: SessionStore, mint: () => Promise<string>) {
  let inflight: Promise<string> | null = null

  return function recover(deadId: string): Promise<string> {
    const current = store.get()
    if (current && current !== deadId) return Promise.resolve(current)

    if (!inflight) {
      store.clear()
      inflight = mint()
        .then((id) => {
          // The store was cleared above, so anything in it now was put there while
          // the mint was in flight: the chat socket, connecting with no id to send,
          // adopting the session the server made for it. A live socket is attached
          // to that one, so it wins and the minted id is dropped. Storing ours on
          // top sent uploads to one session and chat to the other.
          const adopted = store.get()
          if (adopted) return adopted
          store.set(id)
          return id
        })
        .finally(() => {
          inflight = null
        })
    }
    return inflight
  }
}

// ---------------------------------------------------------------------------
// The chat socket and the stored id must be the same session.
//
// REST calls read the stored id on every request, so uploads, dataset changes
// and permissions all land in the session it names. The chat socket is bound to
// one session when it connects and never looks at the store again. If the two
// ever differ, the user uploads a file and then asks about it in a session that
// has never seen it: "I need a dataset for that". The store is the authority (it
// is where the data went), so the socket follows it.
// ---------------------------------------------------------------------------

/** What to do with the session id a socket has just been told it is attached to. */
export type FrameDecision = "same" | "adopt" | "follow"

/**
 * `stored` is what the rest of the app uses now; `heldId` is the id the connection last
 * held: the one it sent when it connected (null if it had none), then the one of each
 * frame it has been told since. A server that replaces a session mid-connection (it
 * evicted the old one) is answering `heldId`, so that case is an `adopt` as well.
 *
 * - `same`: everyone agrees.
 * - `adopt`: nothing newer is stored, so the server's session is the one to use. This
 *   includes the socket having connected with a dead id and been given a new one.
 * - `follow`: something stored a different id after the socket opened. That id is
 *   where the user's data is, so the socket moves to it instead of overwriting it.
 */
export function reconcileSessionFrame(frameId: string, stored: string | null, heldId: string | null): FrameDecision {
  if (stored === frameId) return "same"
  if (!stored || stored === heldId) return "adopt"
  return "follow"
}

export type SocketSync = "ok" | "reattach" | "defer"

/**
 * Whether a socket attached to `attachedTo` should move to the stored id. A turn in
 * flight is never interrupted, so a needed move waits for it ("defer"). A socket that
 * has not yet been told its session, or a store with nothing in it, needs nothing.
 */
export function socketSync(attachedTo: string | null, stored: string | null, turnRunning: boolean): SocketSync {
  if (!attachedTo || !stored || attachedTo === stored) return "ok"
  return turnRunning ? "defer" : "reattach"
}

export type SocketAction = "none" | "adopt" | "reattach"

/**
 * The bookkeeping the chat hook needs, kept out of React so the exact orderings that
 * broke a real upgrade can be replayed in a test. It never touches the store or a
 * socket: it is told what happened and answers with what to do.
 */
export function createSocketSession() {
  let attachedTo: string | null = null
  let heldId: string | null = null
  let stale = false

  function check(stored: string | null, turnRunning: boolean): SocketAction {
    const verdict = socketSync(attachedTo, stored, turnRunning)
    stale = verdict === "defer"
    return verdict === "reattach" ? "reattach" : "none"
  }

  return {
    /** A socket is being opened; `urlId` is the id in its URL. */
    opening(urlId: string | null): void {
      attachedTo = null
      heldId = urlId
      stale = false
    },
    /** The socket closed. */
    closed(): void {
      attachedTo = null
    },
    /** The server said which session this socket is attached to. */
    announced(id: string, stored: string | null, turnRunning: boolean): SocketAction {
      const decision = reconcileSessionFrame(id, stored, heldId)
      attachedTo = id
      heldId = id
      if (decision === "adopt") return "adopt"
      return decision === "follow" ? check(stored, turnRunning) : "none"
    },
    /** The stored id changed, whoever changed it. */
    storeChanged(stored: string | null, turnRunning: boolean): SocketAction {
      return check(stored, turnRunning)
    },
    /**
     * A turn ended (or was cancelled, or an interrupt was handed back); a move that was
     * waiting for it can happen now. `stillBusy` is true when another turn or a queued
     * interrupt is in flight, which keeps the move waiting.
     */
    turnEnded(stored: string | null, stillBusy = false): SocketAction {
      return stale ? check(stored, stillBusy) : "none"
    },
  }
}
