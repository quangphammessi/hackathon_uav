// The fixed LangGraph workflow shape (agentmarket/storefront/graph.py).
// This never changes at runtime -- only which nodes light up, and in what
// order, depends on the trace being replayed.

export interface GraphNodeDef {
  id: string;
  label: string;
  x: number;
  y: number;
  kind: "normal" | "reject" | "success" | "terminal";
}

export interface GraphEdgeDef {
  id: string;
  source: string;
  target: string;
  /** true for the repair -> retriever loop-back edge */
  loop?: boolean;
}

const COL = 210;

export const GRAPH_NODES: GraphNodeDef[] = [
  { id: "planner", label: "planner", x: 0 * COL, y: 140, kind: "normal" },
  { id: "retriever", label: "retriever", x: 1 * COL, y: 140, kind: "normal" },
  { id: "spec_extraction", label: "spec_extraction", x: 2 * COL, y: 140, kind: "normal" },
  { id: "schema_validator", label: "schema_validator", x: 3 * COL, y: 140, kind: "normal" },
  { id: "pricing_agent", label: "pricing_agent", x: 4 * COL, y: 30, kind: "normal" },
  { id: "repair", label: "repair", x: 3.5 * COL, y: 300, kind: "normal" },
  { id: "trust_agent", label: "trust_agent", x: 5 * COL, y: 30, kind: "normal" },
  { id: "response_composer", label: "response_composer", x: 6 * COL, y: -60, kind: "success" },
  { id: "reject", label: "reject", x: 6 * COL, y: 260, kind: "reject" },
  { id: "end", label: "END", x: 7 * COL, y: 100, kind: "terminal" },
];

export const GRAPH_EDGES: GraphEdgeDef[] = [
  { id: "e-planner-retriever", source: "planner", target: "retriever" },
  { id: "e-retriever-spec", source: "retriever", target: "spec_extraction" },
  { id: "e-spec-schema", source: "spec_extraction", target: "schema_validator" },
  { id: "e-schema-pricing", source: "schema_validator", target: "pricing_agent" },
  { id: "e-schema-repair", source: "schema_validator", target: "repair" },
  { id: "e-schema-reject", source: "schema_validator", target: "reject" },
  { id: "e-repair-retriever", source: "repair", target: "retriever", loop: true },
  { id: "e-pricing-trust", source: "pricing_agent", target: "trust_agent" },
  { id: "e-trust-compose", source: "trust_agent", target: "response_composer" },
  { id: "e-trust-reject", source: "trust_agent", target: "reject" },
  { id: "e-compose-end", source: "response_composer", target: "end" },
  { id: "e-reject-end", source: "reject", target: "end" },
];

export const REPLAY_STEP_MS = 350;
