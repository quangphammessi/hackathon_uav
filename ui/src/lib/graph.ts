// The fixed LangGraph workflow shape (services/storefront/graph.py).
// This never changes at runtime -- only which nodes light up, and in what
// order, depends on the trace being replayed.

export interface GraphNodeDef {
  id: string;
  label: string;
  /** one-line description of what this agent does, shown on hover/selection */
  role: string;
  x: number;
  y: number;
  kind: "normal" | "reject" | "success" | "terminal";
  /** the service that executes this node, for the "this is distributed" story */
  service?: "storefront" | "pricing" | "verification";
}

export interface GraphEdgeDef {
  id: string;
  source: string;
  target: string;
  /** true for the repair -> retrieve loop-back edge */
  loop?: boolean;
}

const COL = 205;

export const GRAPH_NODES: GraphNodeDef[] = [
  {
    id: "intent_decode",
    label: "intent_decode",
    role: "Turns the buyer's words into predicates over catalog fields, each one carrying the phrase it came from.",
    x: 0 * COL, y: 140, kind: "normal", service: "storefront",
  },
  {
    id: "retrieve",
    label: "retrieve",
    role: "Two legs: vector similarity, and a structured query that cannot miss a product satisfying every stated requirement.",
    x: 1 * COL, y: 140, kind: "normal", service: "storefront",
  },
  {
    id: "assess",
    label: "assess",
    role: "Scores every candidate against every predicate and keeps the evidence. Values claims are resolved against signed provenance.",
    x: 2 * COL, y: 140, kind: "normal", service: "verification",
  },
  {
    id: "repair",
    label: "repair",
    role: "Widens the search after nothing eligible came back. Never relaxes a stated requirement.",
    x: 2.5 * COL, y: 310, kind: "normal", service: "storefront",
  },
  {
    id: "price",
    label: "price",
    role: "Quotes the item, or the whole kit. Bundle discounts come only from headroom above each component's own floor.",
    x: 3 * COL, y: 30, kind: "normal", service: "pricing",
  },
  {
    id: "verify",
    label: "verify",
    role: "Trust-gates every item about to be offered. Fails closed: 'could not check' and 'it is fine' are different answers.",
    x: 4 * COL, y: 30, kind: "normal", service: "verification",
  },
  {
    id: "compose",
    label: "compose",
    role: "Builds the justification and checks every number and values word in it against the verified fact sheet.",
    x: 5 * COL, y: -55, kind: "success", service: "storefront",
  },
  {
    id: "reject",
    label: "reject",
    role: "Refuses with a machine-readable reason code, the decoded intent, and which candidates failed which requirement.",
    x: 5 * COL, y: 265, kind: "reject", service: "storefront",
  },
  { id: "end", label: "END", role: "The Offer or the RejectedOffer leaves the graph.", x: 6 * COL, y: 105, kind: "terminal" },
];

export const GRAPH_EDGES: GraphEdgeDef[] = [
  { id: "e-intent-retrieve", source: "intent_decode", target: "retrieve" },
  { id: "e-retrieve-assess", source: "retrieve", target: "assess" },
  { id: "e-assess-price", source: "assess", target: "price" },
  { id: "e-assess-repair", source: "assess", target: "repair" },
  { id: "e-assess-reject", source: "assess", target: "reject" },
  { id: "e-repair-retrieve", source: "repair", target: "retrieve", loop: true },
  { id: "e-price-verify", source: "price", target: "verify" },
  { id: "e-price-reject", source: "price", target: "reject" },
  { id: "e-verify-compose", source: "verify", target: "compose" },
  { id: "e-verify-reject", source: "verify", target: "reject" },
  { id: "e-compose-end", source: "compose", target: "end" },
  { id: "e-reject-end", source: "reject", target: "end" },
];

/**
 * Spans that share a trace id but are not nodes of the query graph.
 *
 * A negotiation round is traced under the same trace as the offer it counters,
 * which is what makes the whole conversation reconstructable afterwards -- but
 * it happens after the graph has already returned, so it is rendered in the
 * span list rather than as a node.
 */
export const NON_GRAPH_SPANS = new Set(["negotiate"]);

export const REPLAY_STEP_MS = 350;
