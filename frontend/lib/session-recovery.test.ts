import assert from "node:assert/strict"
import { test } from "node:test"
import { createSessionRecovery, isReplayable, isSessionGone, type SessionStore } from "./session-recovery.ts"

function memoryStore(initial: string | null): SessionStore & { value: string | null } {
  const store = {
    value: initial,
    get: () => store.value,
    set: (id: string) => {
      store.value = id
    },
    clear: () => {
      store.value = null
    },
  }
  return store
}

const BACKEND_MESSAGE = "Session not found or expired. Create a new session and re-upload your data."

test("a 404 only means the session is gone when the backend says so", () => {
  assert.equal(isSessionGone(404, { message: BACKEND_MESSAGE }), true)
  // A missing file or dataset is a 404 too; it must not cost the user their session.
  assert.equal(isSessionGone(404, { message: "File not found." }), false)
  assert.equal(isSessionGone(404, { message: "No dataset named .x. in this session" }), false)
  assert.equal(isSessionGone(500, { message: BACKEND_MESSAGE }), false)
})

test("only reads are replayed after recovery", () => {
  assert.equal(isReplayable(undefined), true)
  assert.equal(isReplayable("get"), true)
  assert.equal(isReplayable("HEAD"), true)
  for (const verb of ["POST", "PUT", "PATCH", "DELETE"]) assert.equal(isReplayable(verb), false)
})

test("the four requests a tab fires on load share one new session", async () => {
  const store = memoryStore("stale")
  let minted = 0
  const recover = createSessionRecovery(store, async () => {
    minted += 1
    await new Promise((resolve) => setTimeout(resolve, 5))
    return "fresh"
  })

  const ids = await Promise.all([recover("stale"), recover("stale"), recover("stale"), recover("stale")])

  assert.deepEqual(ids, ["fresh", "fresh", "fresh", "fresh"])
  assert.equal(minted, 1, "each failed request created its own session")
  assert.equal(store.value, "fresh")
})

test("a session the chat socket already stored is kept, not cleared and replaced", async () => {
  const store = memoryStore("socket-minted")
  let minted = 0
  const recover = createSessionRecovery(store, async () => {
    minted += 1
    return "unwanted"
  })

  assert.equal(await recover("stale"), "socket-minted")
  assert.equal(minted, 0)
  assert.equal(store.value, "socket-minted")
})

test("a late failure after recovery finished reuses the recovered session", async () => {
  const store = memoryStore("stale")
  let minted = 0
  const recover = createSessionRecovery(store, async () => {
    minted += 1
    return "fresh"
  })

  await recover("stale")
  assert.equal(await recover("stale"), "fresh")
  assert.equal(minted, 1)
})

test("a failed mint leaves nothing stuck and the next call tries again", async () => {
  const store = memoryStore("stale")
  let attempts = 0
  const recover = createSessionRecovery(store, async () => {
    attempts += 1
    if (attempts === 1) throw new Error("backend down")
    return "fresh"
  })

  await assert.rejects(recover("stale"), /backend down/)
  assert.equal(store.value, null, "the dead id must not be kept")
  assert.equal(await recover("stale"), "fresh")
  assert.equal(store.value, "fresh")
})
