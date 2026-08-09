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
1. **Analysis Depth**: Auto / Fast / Deep segmented control.
2. **Permission Profile**: `components/chat/permission-control.tsx` popover.

A popover is used instead of a third segmented control group to prevent visual
crowding. The full per-category permission matrix lives on `/settings`.

### Mid-Run Permission Prompts

**A permission prompt does not end the turn.**
When an `approval_required` frame carries an `id`, `use-chat-stream.ts` keeps
`isRunning=true` and preserves `activeIdRef`. `respondToApproval` replies in
place over the existing turn rather than starting a new turn.

---

## Trust Layer & Skill Credit Rendering

- `components/chat/investigation-trail.tsx` renders granular agent steps.
- `components/chat/answer-trust.tsx` renders confidence and verification checks.
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
