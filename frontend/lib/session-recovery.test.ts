import assert from "node:assert/strict"
import { test } from "node:test"
import {
  createSessionRecovery,
  createSocketSession,
  isReplayable,
  isSessionGone,
  reconcileSessionFrame,
  socketSync,
  type SessionStore,
  type SocketAction,
} from "./session-recovery.ts"

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

test("a session the chat socket stores while the mint is in flight wins over the minted one", async () => {
  // The order in a real upgrade: the four reads fail, recovery clears the dead id and
  // starts minting, and in the same instant the chat socket (which then had no id to
  // send) connects and adopts the session the server made for it. Recovery used to
  // store its own id on top, so uploads went to one session and chat to the other.
  const store = memoryStore("stale")
  const recover = createSessionRecovery(store, async () => {
    store.set("socket-minted")
    await new Promise((resolve) => setTimeout(resolve, 5))
    return "recovery-minted"
  })

  assert.equal(await recover("stale"), "socket-minted")
  assert.equal(store.value, "socket-minted", "recovery overwrote the session the socket is attached to")
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

// ---------------------------------------------------------------------------
// The chat socket follows the stored id. The two orderings below are the ones a
// backend restart produces: the four reads fail, recovery starts minting, and in
// the same instant the chat socket connects with no id to send.
// ---------------------------------------------------------------------------

test("a socket's session frame never overwrites a newer stored id, and never loops on a dead one", () => {
  // Nothing stored, or the id the socket opened with was replaced by the server: use the server's.
  assert.equal(reconcileSessionFrame("s1", null, null), "adopt")
  assert.equal(reconcileSessionFrame("new", "dead", "dead"), "adopt")
  // Everyone already agrees.
  assert.equal(reconcileSessionFrame("s1", "s1", null), "same")
  assert.equal(reconcileSessionFrame("s1", "s1", "s1"), "same")
  // Something stored a different id after the socket opened: that is where the data is.
  assert.equal(reconcileSessionFrame("socket", "rest", null), "follow")
  assert.equal(reconcileSessionFrame("socket", "rest", "older"), "follow")
})

test("a turn in flight is never interrupted to move the socket", () => {
  assert.equal(socketSync("a", "b", false), "reattach")
  assert.equal(socketSync("a", "b", true), "defer")
  assert.equal(socketSync("a", "a", false), "ok")
  assert.equal(socketSync(null, "b", false), "ok", "a socket not yet told its session has nothing to move")
  assert.equal(socketSync("a", null, false), "ok")
})

/** A tab: one store, one chat socket. Reattaching reopens the socket with whatever the store holds. */
function tab(initial: string | null) {
  const store = memoryStore(initial)
  const socket = createSocketSession()
  const actions: SocketAction[] = []
  let attached: string | null = null

  function open() {
    socket.opening(store.value)
    attached = null
  }
  function announce(id: string, running = false) {
    const action = socket.announced(id, store.value, running)
    attached = id
    actions.push(action)
    if (action === "adopt") store.set(id)
    if (action === "reattach") {
      open()
      announce(store.value as string)
    }
  }
  function storeChanged(running = false) {
    const action = socket.storeChanged(store.value, running)
    actions.push(action)
    if (action === "reattach") {
      open()
      announce(store.value as string)
    }
  }
  return { store, socket, actions, open, announce, storeChanged, attachedTo: () => attached }
}

test("upgrade race, socket first: the session the socket adopted is the one recovery keeps", async () => {
  const t = tab("dead")
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const recover = createSessionRecovery(t.store, async () => {
    await gate
    return "recovery-minted"
  })

  const recovered = recover("dead") // clears the store and starts minting
  t.open() // the chat socket connects with no id
  t.announce("socket-minted") // the server made it a session; nothing newer is stored
  release()

  assert.equal(await recovered, "socket-minted")
  assert.equal(t.store.value, "socket-minted")
  assert.equal(t.attachedTo(), "socket-minted", "uploads and chat must be the same session")
  assert.deepEqual(t.actions, ["adopt"])
})

test("upgrade race, recovery first: the socket moves to the session the data went to", async () => {
  const t = tab("dead")
  const recover = createSessionRecovery(t.store, async () => "recovery-minted")

  const recovered = recover("dead")
  t.open() // connects with no id
  await recovered // recovery stores its id; the socket has not been told its session yet
  t.storeChanged()
  t.announce("socket-minted") // the frame arrives late; the store already holds the id uploads use

  assert.equal(t.store.value, "recovery-minted", "the socket's session must not overwrite the stored one")
  assert.equal(t.attachedTo(), "recovery-minted", "the socket stayed on a session no upload ever reached")
  // Store change with no frame yet, then the frame says "follow" so the socket reattaches, then the new frame agrees.
  assert.deepEqual(t.actions, ["none", "reattach", "none"])
})

test("a socket that opened with a dead id adopts the server's replacement and does not chase the dead one", () => {
  const t = tab("dead")
  t.open() // sends ?session=dead; the server makes a new session and says so
  t.announce("fresh")

  assert.equal(t.store.value, "fresh")
  assert.equal(t.attachedTo(), "fresh")
  assert.deepEqual(t.actions, ["adopt"], "must not try to reattach to the dead id")
})

test("any later change to the stored id moves an idle socket, and waits for a running turn", () => {
  const t = tab("a")
  t.open()
  t.announce("a")

  t.store.set("b") // some other writer: the server minted a session for a header-less request
  t.storeChanged(true) // a turn is running
  assert.equal(t.attachedTo(), "a", "a running turn must not be moved")

  const later = t.socket.turnEnded(t.store.value)
  assert.equal(later, "reattach", "the move that waited for the turn must happen when it ends")
  t.actions.push(later)
  t.open()
  t.announce("b")
  assert.equal(t.attachedTo(), "b")

  assert.equal(t.socket.turnEnded(t.store.value), "none", "nothing is waiting any more")
})

test("a change that reverts before the turn ends leaves nothing to do", () => {
  const t = tab("a")
  t.open()
  t.announce("a")
  t.store.set("b")
  t.storeChanged(true)
  t.store.set("a") // back to what the socket is on

  assert.equal(t.socket.turnEnded(t.store.value), "none")
})

test("a closed socket has nothing to move", () => {
  const t = tab("a")
  t.open()
  t.announce("a")
  t.socket.closed()
  t.store.set("b")

  assert.equal(t.socket.storeChanged(t.store.value, false), "none", "the reconnect reads the store itself")
})

test("a server that replaces the session mid-connection is adopted, not chased", () => {
  const t = tab(null)
  t.open() // no id to send; the server makes A
  t.announce("A")
  assert.equal(t.store.value, "A")

  // The server evicts A and answers the next frame with B. The socket held A, so this is a replacement.
  t.announce("B")
  assert.equal(t.store.value, "B")
  assert.equal(t.attachedTo(), "B")

  // And again: still one hop, never a reattach to a dead id.
  t.announce("C")
  assert.equal(t.store.value, "C")
  assert.deepEqual(t.actions, ["adopt", "adopt", "adopt"])
  assert.equal(reconcileSessionFrame("B", "A", "A"), "adopt")
})

test("a replacement is still a follow when something else stored a different id meanwhile", () => {
  const t = tab(null)
  t.open()
  t.announce("A")
  t.store.set("data") // an upload went to another session
  t.storeChanged(false)
  assert.equal(t.attachedTo(), "data")
  assert.deepEqual(t.actions, ["adopt", "reattach", "none"])
})

test("a queued interrupt keeps a move waiting the same way a running turn does", () => {
  const t = tab("a")
  t.open()
  t.announce("a")
  t.store.set("b")
  t.storeChanged(true)

  // The turn ended but an interrupt is still queued: nothing moves yet, and it is still owed.
  assert.equal(t.socket.turnEnded(t.store.value, true), "none")
  // The interrupt was handed back (or cancelled): now it can.
  assert.equal(t.socket.turnEnded(t.store.value, false), "reattach")
  assert.equal(t.socket.turnEnded(t.store.value, false), "none", "and only once")
})

test("stopping a turn settles the move it was holding back", () => {
  const t = tab("a")
  t.open()
  t.announce("a")
  t.store.set("b") // recovery stored a new session while a turn was running
  t.storeChanged(true)
  // Stop: no frame ends the turn on the client, but the hook reports it as ended all the same.
  assert.equal(t.socket.turnEnded(t.store.value, false), "reattach")
})
