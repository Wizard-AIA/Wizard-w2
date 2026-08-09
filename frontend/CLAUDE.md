# Frontend

Frontend-specific architecture, commands and UI conventions.
Loads only when work touches `frontend/`. Global rules: [root CLAUDE.md](../CLAUDE.md).

## Commands

```bash
cd frontend && pnpm install
pnpm dev                                         # Next.js dev server (:3000)
pnpm lint && npx tsc --noEmit && pnpm build      # The three CI gates
```

## Routes

Five routes, no landing page — `/` **is** the workspace:

| Route | Component | Purpose |
|---|---|---|
| `/` | [components/chat-shell.tsx](components/chat-shell.tsx) | Chat workspace & agent execution |
| `/data` | [components/pages/data-workbench.tsx](components/pages/data-workbench.tsx) | Datasets & per-source cloud policy |
| `/skills` | [components/pages/skills-workbench.tsx](components/pages/skills-workbench.tsx) | Skills browser, history & promotion |
| `/models` | [components/pages/models-workbench.tsx](components/pages/models-workbench.tsx) | Model selection & downloading |
| `/settings` | [components/pages/settings-workbench.tsx](components/pages/settings-workbench.tsx) | System profile & permission matrix |

## Invariants & Rules

### Navigation & Shell
- [components/app-shell.tsx](components/app-shell.tsx) renders the nav rail **once** from the root layout. Do not mount per page (would tear down and rebuild the chat WebSocket on route changes).
- [components/data-mode-control.tsx](components/data-mode-control.tsx) sits in the nav rail with the live cost readout (`lib/usage-store.ts`).

### WebSocket & Streaming
- [lib/use-chat-stream.ts](lib/use-chat-stream.ts) manages the persistent chat WebSocket.
- **Every socket handler must check `socketRef.current === socket`** to prevent duplicate/orphan socket leaks under React StrictMode effect remounts.
- `connect()` must perform **no synchronous setState** in mount effects (triggers ESLint error).
- Session ID is persisted in `localStorage` and sent on every request via `X-Session-Id`.

### Permission & Prompts
- The composer has two independent controls: Depth segmented control (Auto/Fast/Deep) and Permission popover ([components/chat/permission-control.tsx](components/chat/permission-control.tsx)).
- **Permission prompts do not end the turn.** When `approval_required` carries an `id`, `use-chat-stream.ts` preserves `isRunning=true` and `activeIdRef`; `respondToApproval` replies in place.
- `ChatMessage.instruction` is stamped when a turn is sent.

### Trust & Attribution
- [components/chat/investigation-trail.tsx](components/chat/investigation-trail.tsx) and [components/chat/answer-trust.tsx](components/chat/answer-trust.tsx) are collapsed by default.
- [components/chat/skill-credit.tsx](components/chat/skill-credit.tsx) sits with trust surfaces; links to `/skills`. `skill` frames are deduped by name.

### Design System — [app/globals.css](app/globals.css)
- **Use tokens only.** Every color, shadow, duration, and easing curve is a token in `globals.css`. Never add raw `#hex` or arbitrary duration utilities.
- **Light only.** Do not add `dark:` utility classes (surfaces are tuned for warm white ground).
- Base background is painted on `html`, not `body` (so `.aurora` fixed background is visible).
- Font is **Geist**, self-hosted from the `geist` npm package (no build-time Google Fonts downloads).
- Motion: `.reveal`, `.reveal-in`, `.reveal-scale`, `.lift`, `.caret`. `prefers-reduced-motion` resets animations to completed end states.

## Deep Documentation

For deep UI architecture, React StrictMode analysis, and design system rationale:
- [docs/frontend.md](../docs/frontend.md) — Route architecture, WebSocket lifecycle, component state patterns, and design tokens
