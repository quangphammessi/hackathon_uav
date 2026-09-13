"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Position,
  Handle,
  type Node,
  type Edge,
  type NodeProps,
  MarkerType,
} from "@xyflow/react";
import { GRAPH_NODES, GRAPH_EDGES, REPLAY_STEP_MS, type GraphNodeDef } from "@/lib/graph";
import type { TraceSpan } from "@/lib/api";

type Tone = "dim" | "normal" | "reject" | "success";

interface AmNodeData {
  def: GraphNodeDef;
  executed: boolean;
  tone: Tone;
  active: boolean;
  count: number;
  totalDurationMs: number | null;
  onSelect?: () => void;
  [key: string]: unknown;
}

const TONE_STYLES: Record<Tone, string> = {
  dim: "border-border-soft bg-bg-panel text-text-faint opacity-70",
  normal: "border-accent/70 bg-accent/10 text-text",
  reject: "border-err/70 bg-err/10 text-err",
  success: "border-ok/70 bg-ok/10 text-ok",
};

const RING_STYLES: Record<Tone, string> = {
  dim: "",
  normal: "ring-2 ring-accent/60 shadow-[0_0_16px_rgba(94,234,212,0.35)]",
  reject: "ring-2 ring-err/60 shadow-[0_0_16px_rgba(251,113,133,0.35)]",
  success: "ring-2 ring-ok/60 shadow-[0_0_16px_rgba(52,211,153,0.35)]",
};

function AmNode({ data }: NodeProps<Node<AmNodeData>>) {
  const { def, executed, tone, active, count, totalDurationMs, onSelect } = data;
  const isTerminal = def.kind === "terminal";
  const clickable = executed && !isTerminal && !!onSelect;

  return (
    <div
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? onSelect : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") onSelect?.();
            }
          : undefined
      }
      className={[
        "rounded-lg border px-3 py-2 font-mono text-[11px] leading-tight transition-all duration-300",
        isTerminal ? "min-w-[64px] text-center" : "min-w-[152px]",
        TONE_STYLES[tone],
        active ? RING_STYLES[tone] : "",
        clickable ? "cursor-pointer hover:brightness-125" : "cursor-default",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">{def.label}</span>
        {count > 1 && (
          <span className="rounded bg-black/30 px-1 text-[10px] text-text-dim">
            &times;{count}
          </span>
        )}
      </div>
      {executed && totalDurationMs !== null && !isTerminal && (
        <div className="mt-0.5 text-[10px] text-text-dim">
          {totalDurationMs.toFixed(2)} ms
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { amNode: AmNode };

export interface GraphViewProps {
  spans: TraceSpan[] | null;
  onSelectNode?: (nodeId: string, occurrences: TraceSpan[]) => void;
}

export function GraphView({ spans, onSelectNode }: GraphViewProps) {
  const [revealedCount, setRevealedCount] = useState(0);
  const [endLit, setEndLit] = useState(false);

  useEffect(() => {
    setRevealedCount(0);
    setEndLit(false);
    if (!spans || spans.length === 0) return;
    const timers: ReturnType<typeof setTimeout>[] = [];
    for (let k = 1; k <= spans.length; k++) {
      timers.push(setTimeout(() => setRevealedCount(k), k * REPLAY_STEP_MS));
    }
    timers.push(
      setTimeout(() => setEndLit(true), (spans.length + 1) * REPLAY_STEP_MS),
    );
    return () => timers.forEach(clearTimeout);
  }, [spans]);

  const { nodes, edges } = useMemo(() => {
    const revealed = spans?.slice(0, revealedCount) ?? [];
    const occurrencesByNode: Record<string, TraceSpan[]> = {};
    for (const s of revealed) {
      (occurrencesByNode[s.name] ??= []).push(s);
    }

    const traversed = new Set<string>();
    let currentEdgeId: string | null = null;
    for (let i = 1; i < revealed.length; i++) {
      const edge = GRAPH_EDGES.find(
        (e) => e.source === revealed[i - 1].name && e.target === revealed[i].name,
      );
      if (edge) {
        traversed.add(edge.id);
        if (i === revealed.length - 1) currentEdgeId = edge.id;
      }
    }

    let endTone: "success" | "reject" | null = null;
    let latestNodeId: string | null =
      revealed.length > 0 ? revealed[revealed.length - 1].name : null;

    if (spans && revealed.length === spans.length && revealed.length > 0) {
      const last = revealed[revealed.length - 1].name;
      if (last === "response_composer") {
        endTone = "success";
        if (endLit) {
          traversed.add("e-compose-end");
          currentEdgeId = "e-compose-end";
          latestNodeId = "end";
        }
      } else if (last === "reject") {
        endTone = "reject";
        if (endLit) {
          traversed.add("e-reject-end");
          currentEdgeId = "e-reject-end";
          latestNodeId = "end";
        }
      }
    }

    const rfNodes: Node<AmNodeData>[] = GRAPH_NODES.map((def) => {
      const occurrences = occurrencesByNode[def.id] ?? [];
      let executed = occurrences.length > 0;
      let tone: Tone = "dim";
      if (def.id === "end") {
        executed = endLit;
        tone = executed ? (endTone === "reject" ? "reject" : "success") : "dim";
      } else if (executed) {
        tone = def.id === "reject" ? "reject" : def.id === "response_composer" ? "success" : "normal";
      }
      const totalDurationMs = occurrences.length
        ? occurrences.reduce((sum, s) => sum + s.duration_ms, 0)
        : null;
      return {
        id: def.id,
        type: "amNode",
        position: { x: def.x, y: def.y },
        data: {
          def,
          executed,
          tone,
          active: latestNodeId === def.id,
          count: occurrences.length,
          totalDurationMs,
          onSelect:
            occurrences.length > 0
              ? () => onSelectNode?.(def.id, occurrences)
              : undefined,
        },
        draggable: false,
        connectable: false,
      };
    });

    const rfEdges: Edge[] = GRAPH_EDGES.map((e) => {
      const isTraversed = traversed.has(e.id);
      const isCurrent = currentEdgeId === e.id;
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        type: "smoothstep",
        animated: isCurrent,
        style: {
          stroke: isTraversed ? "var(--accent-strong)" : "var(--border)",
          strokeWidth: isTraversed ? 2 : 1,
          opacity: isTraversed ? 1 : 0.45,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: isTraversed ? "var(--accent-strong)" : "var(--border)",
          width: 14,
          height: 14,
        },
        pathOptions: { borderRadius: 12, offset: e.loop ? 24 : 0 },
        zIndex: e.loop ? 10 : 0,
      };
    });

    return { nodes: rfNodes, edges: rfEdges };
  }, [spans, revealedCount, endLit, onSelectNode]);

  return (
    <div className="h-[440px] w-full overflow-hidden rounded-lg border border-border bg-bg-panel">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
        zoomOnDoubleClick={false}
        minZoom={0.4}
        maxZoom={1.5}
      >
        <Background color="var(--border-soft)" gap={24} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
