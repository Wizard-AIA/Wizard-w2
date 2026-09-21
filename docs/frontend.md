# Frontend Architecture & UI Reference

> Deep reference for the Wizard w2 Next.js frontend, state management, WebSocket lifecycle,
> and design system.
> Concise rules live in [`frontend/CLAUDE.md`](../frontend/CLAUDE.md).

---

## Route Structure

Five routes, no separate landing page — `/` **is** the workspace:

| Route | Component | Purpose |
|---|---|---|
| `/` | `components/chat-shell.tsx` | Main conversation & agent execution workspace |
| `/data` | `components/pages/data-workbench.tsx` | Dataset inspection, schema viewing, per-source cloud policy |
| `/skills` | `components/pages/skills-workbench.tsx` | Skill browser, usage history, promotion review, manual editing |
| `/models` | `components/pages/models-workbench.tsx` | Model selector, model downloader, API key manager |
| `/settings` | `components/pages/settings-workbench.tsx` | System profile, inference facts, full permission matrix, diagnostics |

`components/app-shell.tsx` renders the navigation rail once from the root layout.
**It must mount once from root**: mounting per page would tear down and rebuild
the chat WebSocket on every route change.

---

## WebSocket & Streaming Lifecycle — `lib/use-chat-stream.ts`

The event-to-state reduction for the chat message lives in `lib/turn-state.ts`. It acts as a pure state machine replicating the backend's state definitions.
- **Optimistic Send Phase**: `"routing"`.
- **Terminal States**: A run ends only on `final`, `error`, `cancelled`, or an `approval_required` frame without an `id` (plan gate). Late-arriving frames (like an `observation` sent out of order after `final`) are discarded.
- **Conversation route (`route.workflow === "converse"`)**: Handled silently without rendering the investigation trail, timeline, or empty plan panel.
- **Cancellation**: Intercepts `Stop` events mapping the `cancelled` frame directly to the `cancelled` UI phase, with immediate unblocking of the composer.
- **Chat Before Data**: The composer allows sending text without a dataset loaded. If a response requires data (`route.needs_data`), the UI renders an inline file upload affordance.


`use-chat-stream.ts` owns one persistent WebSocket with heartbeat and
exponential-backoff reconnect, appending each `*_delta` frame to the live message.

### React StrictMode & Socket Deduping

**Every socket handler first checks it is still the socket the hook holds**
(`socketRef.current === socket`).

In development, React StrictMode remounts every effect on initial load. A discarded
socket otherwise keeps acting as the live one: its `onclose` nulls `socketRef`
out from under the replacement and schedules another reconnect, leaving an orphan
socket open. Because `ws_gate` caps connections at `WS_MAX_CONCURRENT_PER_IP` (4),
one tab would consume two slots.

Retiring a socket also requires detaching its handlers, and for a socket still in
`CONNECTING`, closing on open rather than immediately — calling `close()`
mid-handshake produces "WebSocket is closed before the connection is established"
browser errors.

---

## Composer & Permission Controls

The composer holds **two independent dials**:
1. **Analysis Depth**: Auto / Fast / Deep segmented control. A preference that stays until changed. Auto lets the agent decide depth; Fast is a single pass without verification; Deep always investigates and verifies.
2. **Permission Profile**: `components/chat/permission-control.tsx` popover.

A popover is used instead of a third segmented control group to prevent visual
crowding. The full per-category permission matrix lives on `/settings`.

### Session Ownership: Socket and REST Must Agree

The chat socket is bound to one backend session when it connects. Every REST call
(uploads, dataset changes, permissions) uses whatever id is stored in
`localStorage` at that moment. If the two ever differ, a user uploads a file and
then asks about it in a session that has never seen it, and the router correctly
answers "I need a dataset for that."

After a backend restart a tab left open has a dead id. Its four load-time reads
fail with `404 Session not found`, recovery starts minting a session, and in the same
instant the socket connects with no id and adopts a different session the server
makes for it. Both cannot be right, so the rules are:

| Event | Rule |
|---|---|
| Recovery's mint returns and the store now holds an id | Keep the stored one (a live socket is attached to it); drop the minted one. |
| The socket is told its session, nothing newer stored (or the id the connection held was replaced, at connect or later by an eviction) | Adopt the server's id. Never chase a dead id: that would loop. |
| The socket is told its session, and a different id was stored after it opened | The stored id holds the user's data; the socket moves to it. |
| The stored id changes for any reason, including another tab writing it (`storage` event) | An idle socket reattaches to it. A running turn or a queued interrupt is never interrupted; the move waits until neither is in flight. |
| A turn stops any way at all: its last frame, Stop, clearing the chat, a send that never left | `setRunning(false)` schedules the waiting move, so it is never stranded. |

The decisions are the pure `createSocketSession` in `lib/session-recovery.ts`, tested
by replaying both orderings a restart can produce. `use-chat-stream.ts` only
executes them, and `onSessionIdChange` in `lib/api.ts` tells it when the stored id
changes, and also when another tab writes the key. Not covered by a unit test
(`api.ts` cannot load under Node's test runner): the `storage` listener itself, and
the hook's wiring of `setRunning` to the move; both are verified by reading and by
the lint, type and build gates only.

### Interruption & Draft Recovery

- **Enter while running**: Pressing Enter while a turn runs submits the text via `interrupt`. The backend can reject it if busy, passing the input back via `restoredDraft`.
- **Restored Draft**: When an interruption is refused and returned via `restoredDraft`, the composer refills automatically if it's empty.

### Mid-Run Permission Prompts

**A permission prompt does not end the turn.**
When an `approval_required` frame carries an `id`, `use-chat-stream.ts` keeps
`isRunning=true` and preserves `activeIdRef`. `respondToApproval` replies in
place over the existing turn rather than starting a new turn.

---

## Trust Layer & Skill Credit Rendering

- `components/chat/investigation-trail.tsx` renders granular agent steps.
- `components/chat/answer-trust.tsx` renders confidence and verification checks.
- **Route Chip**: Each finished message (except conversation and failed/cancelled) shows a route chip indicating how the turn was routed (e.g. 'Investigated', 'Answered directly', 'Planned, then investigated', 'Plan only', 'Ran the approved plan').
- `components/chat/skill-credit.tsx` sits beside trust surfaces, rendering which
  skills informed the analysis with direct links to `/skills`.
- `skill` frames are deduped by name in the hook.
- Grounding and verification arrive twice (as structured fields and warning
  strings); `message.tsx` filters structured warning prefixes to avoid repetition.

---

## Promotion & Save-As-Skill

`components/chat/skill-promotion.tsx` serves both promotion flows:
1. **Agent Threshold Offer**: Triggered by `skill_candidate` frame.
2. **User "Save as skill"**: Triggered via action button on finished message.

Drafts are fetched from the backend (derived from real plan and code), not
composed client-side. Declining an offer is persisted server-side so it is not
repeated.

---

## Live State & Store Subscriptions

- **Data Mode Control**: Located in `components/data-mode-control.tsx` inside the
  nav rail with live session cost readout.
- **`useSyncExternalStore` Pattern**: Used for live cost updates (`lib/usage-store.ts`)
  and audio state (`lib/use-sound.ts`) to avoid `react-hooks/set-state-in-effect`
  lint errors during render.
- **Session Persistence**: Session ID is stored in `localStorage` and passed via
  headers (`X-Session-Id`), allowing browser reloads to reconnect to the active session.

---

## Design System Tokens — `app/globals.css`

Every color, shadow, duration, and easing curve is defined as a CSS token.

### Core Rules

- **Light Only**: The UI is intentionally tuned for warm white ground surfaces.
  Do not add `dark:` variant classes.
- **HTML Base**: Base background is set on `html`, not `body` (so `.aurora` fixed
  background is not occluded).
- **Typography**: Uses the self-hosted `geist` npm package (avoiding build-time
  external font downloads).
- **Motion Tokens**: `.reveal`, `.reveal-in`, `.reveal-scale`, `.lift`, `.caret`.
  `prefers-reduced-motion` resets animations to their completed end state.

---

## Sound & Brand Assets

- `components/animated-orb.tsx`: Brand mark with size-computed blur and drop shadows.
- `lib/use-sound.ts`: Pools one `Audio` element per sound (reused across clicks).
- **Autoplay Handling**: Browser gesture requirements are handled by re-arming
  audio playback on the first user interaction.
