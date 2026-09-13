# AgentMarket OS -- UI

The Next.js front end for AgentMarket OS (UAVS Hackathon 2026, FPT
Australasia challenge: *"The B2A Shift: Adapting Retail for AI Shopping
Agents"*). This app is purely a client for the Python gateway in the repo
root -- it holds no business logic of its own.

## Stack

Next.js 15 (App Router, TypeScript), Tailwind CSS, Recharts, React Flow
(`@xyflow/react`). No auth library, no state library beyond React hooks.

## Run it

```bash
npm install
cp .env.example .env.local   # adjust the gateway URL if needed
npm run dev                  # http://localhost:3000
```

The gateway (see repo root README) must be running and reachable at the
configured URL -- start it with:

```bash
uvicorn agentmarket.gateway.api:app --reload --port 8080
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_GATEWAY_URL` | `http://localhost:8080` | Base URL of the FastAPI gateway. Read everywhere through `src/lib/api.ts`; the gateway sends permissive CORS headers so any reachable host:port works. Baked into the client bundle at build time -- changing it after a Docker build requires rebuilding the image. |

## Pages

- **`/` -- Agent Demo Console.** Simulates an autonomous buyer agent: it
  requests an agent token, runs a free-text (or one of four preset) query
  against `/v1/query`, and renders the resulting Offer or Rejection. The
  centerpiece is a React Flow rendering of the actual LangGraph workflow
  (`planner -> retriever -> spec_extraction -> schema_validator -> pricing_agent
  | repair | reject -> trust_agent -> response_composer | reject`), replayed
  span-by-span from `/v1/trace/{trace_id}` with a staggered animation.
  Clicking a lit node shows that span's recorded attributes as JSON --
  direct proof the pipeline reasoned over real data. When an Offer is
  returned, a "Sign mandates & pay" button walks through the AP2 mandate
  chain (Intent Mandate -> Cart Mandate -> Settlement) via
  `/v1/principals/intent-mandate` and `/v1/pay`. Visiting `/?trace=<id>`
  (e.g. from an Ops Dashboard trace link) loads and replays that archived
  trace directly.

- **`/ops` -- Ops Dashboard.** A single call to `/v1/ops/overview`
  populates: a service health strip (with expandable backend detail per
  service), a pricing table with a per-SKU competitor-price chart
  (Recharts, with reference lines for fair value / MAP / margin floor) and
  bandit-arm performance, a provenance & trust table (the SKU with a
  deliberate `CHAIN_GAP` is called out), ledger integrity with a
  hash-chain-intact indicator and a "Verify now" re-check, recent orders,
  recent traces (linking back into `/` for replay), and a live event feed
  (Server-Sent Events via `/v1/events/stream`, falling back to polling
  `/v1/events` if the stream errors).

## Build

```bash
npm run build
npm run start
```

`next.config.ts` sets `output: "standalone"` for the Docker image below.

## Docker

```bash
docker build -t agentmarket-ui --build-arg NEXT_PUBLIC_GATEWAY_URL=http://gateway:8080 .
docker run -p 3000:3000 agentmarket-ui
```

Multi-stage, non-root, exposes port 3000. Since `NEXT_PUBLIC_*` variables
are inlined into the client bundle at build time, pass the gateway URL as a
build arg (not just an env var at `docker run`) if it differs from the
default.

## Project structure

```
src/
  app/
    layout.tsx        shared shell: nav, footer, theme
    page.tsx           Agent Demo Console entry (wraps AgentConsole in Suspense)
    ops/page.tsx        Ops Dashboard
    globals.css         design tokens, dark theme, React Flow overrides
  components/
    AgentConsole.tsx    all Agent Demo Console state/logic
    GraphView.tsx        the LangGraph React Flow visualization + replay animation
    OfferCard.tsx, RejectionCard.tsx, MandateFlow.tsx, SpanDetailPanel.tsx
    StatusBadge.tsx      shared badge/dot primitives
    ops/                 one component per Ops Dashboard section
  lib/
    api.ts               the ONLY place that calls the gateway; all response types live here
    graph.ts              the fixed LangGraph node/edge layout
```
