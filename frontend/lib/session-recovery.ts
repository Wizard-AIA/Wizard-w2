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
