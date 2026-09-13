import type { TraceSpan } from "@/lib/api";

export function SpanDetailPanel({
  nodeId,
  spans,
  onClose,
}: {
  nodeId: string;
  spans: TraceSpan[];
  onClose: () => void;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-panel">
      <div className="flex items-center justify-between border-b border-border-soft px-4 py-2.5">
        <div>
          <div className="font-mono text-xs text-text-dim">node.attributes</div>
          <div className="font-mono text-sm font-semibold text-accent">{nodeId}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-border px-2 py-1 text-xs text-text-dim hover:bg-bg-raised hover:text-text"
          aria-label="Close span detail panel"
        >
          Close
        </button>
      </div>
      <div className="max-h-[320px] space-y-3 overflow-y-auto p-4 scrollbar-thin">
        {spans.map((span, i) => (
          <div key={span.span_id} className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-text-dim">
              {spans.length > 1 && <span className="text-accent">call #{i + 1}</span>}
              <span>span_id: {span.span_id}</span>
              <span>status: {span.status}</span>
              <span>{span.duration_ms.toFixed(3)} ms</span>
            </div>
            <pre className="overflow-x-auto rounded-md border border-border-soft bg-bg p-3 font-mono text-[11px] leading-relaxed text-text scrollbar-thin">
              {JSON.stringify(span.attributes, null, 2)}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
