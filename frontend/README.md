# Wizard — frontend

The client for [Wizard](../Readme.md): a Next.js 16 app that opens straight into
the workspace. There is no marketing page in front of it — this is a tool you
open, not a product you are sold.

## Run it

```bash
pnpm install
pnpm dev                # http://localhost:3000

pnpm lint && npx tsc --noEmit && pnpm build   # the three CI gates
```

The backend is expected at `http://localhost:8000`. Point elsewhere with
`NEXT_PUBLIC_API_URL`.

## Pages

| Route       | What it is                                                          |
|-------------|---------------------------------------------------------------------|
| `/`         | The conversation: streamed reasoning, generated code, live output    |
| `/data`     | Datasets and reference documents — load, inspect, switch, remove     |
| `/models`   | Every provider and installed model, and which role each one fills    |
| `/settings` | Session controls, interface preferences, resolved server diagnostics |

[`app-shell.tsx`](components/app-shell.tsx) renders the rail once in the root
layout, so moving between pages never rebuilds it and the chat WebSocket is
never disturbed by a route change.

## Streaming

[`use-chat-stream.ts`](lib/use-chat-stream.ts) owns one persistent WebSocket with
a heartbeat and exponential-backoff reconnect, appending each `*_delta` frame to
the live message. This is real token streaming — do not reintroduce a
timer-based word reveal on top of it.

The backend is a loop, not a pipeline, so alongside the delta frames it emits
`iteration_start`, `action`, `observation`, `finding`, `plan_revised`,
`assumption` and `verification`. An `observation` closes the most recent `action`
that has none — the two are never correlated by id, which keeps the protocol
one-way. [`chat/investigation-trail.tsx`](components/chat/investigation-trail.tsx)
renders what the agent chose to do; [`chat/answer-trust.tsx`](components/chat/answer-trust.tsx)
renders how far the answer can be trusted. Both collapse by default: the answer
is the headline, these are the evidence.

Grounding and verification arrive **twice** — once as a warning string, for REST
clients that have no richer surface, and once as structured fields. `message.tsx`
filters the two known prefixes out of the plain warning list so nothing is shown
twice; those prefixes are coupled to the backend strings that produce them.

The session id lives in `localStorage` and is sent on every request, so a reload
rejoins the same server-side session, dataset and sandbox container.

## Design system

All of it lives in [`app/globals.css`](app/globals.css) as tokens — colour,
elevation, duration, easing. Components reference tokens, never raw values.

- **Light only.** There is no dark palette and no `dark` variant; the ambient
  washes, the orb's glow and the shadow ramp are all tuned against a warm white
  ground. `dark:` classes will silently do nothing.
- **Type** is [Geist](https://vercel.com/font), self-hosted from the `geist`
  package. Deliberately not `next/font/google`: that downloads at build time,
  which would make `npm run build` — a CI gate — fail whenever Google Fonts was
  unreachable.
- **Motion** is blur plus a small rise, never a long slide. `prefers-reduced-motion`
  neutralises entrances to their end state rather than merely shortening them.

## Stack

Next.js 16 (App Router, Turbopack) · React 19 · Tailwind CSS v4 · Radix
primitives · lucide-react · Geist.

Charts are produced by the **backend** — matplotlib or Plotly HTML, rendered in
the artifacts panel. There is no client-side charting library.
