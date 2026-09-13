import Link from "next/link";
import type { TraceSummary } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export function TracesTable({ traces }: { traces: TraceSummary[] }) {
  if (traces.length === 0) {
    return <p className="text-sm text-text-faint">No traces recorded yet.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[680px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">Trace</th>
            <th className="px-3 py-2 font-medium">Query</th>
            <th className="px-3 py-2 font-medium">Spans</th>
            <th className="px-3 py-2 font-medium">Duration</th>
            <th className="px-3 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <tr key={t.trace_id} className="border-b border-border-soft last:border-0 hover:bg-bg-raised">
              <td className="px-3 py-2">
                <Link
                  href={`/?trace=${encodeURIComponent(t.trace_id)}`}
                  className="font-mono text-accent hover:underline"
                >
                  {t.trace_id}
                </Link>
              </td>
              <td className="max-w-[280px] truncate px-3 py-2 text-text-dim">
                {t.query}
              </td>
              <td className="px-3 py-2 font-mono">{t.spans}</td>
              <td className="px-3 py-2 font-mono">{t.total_ms.toFixed(1)} ms</td>
              <td className="px-3 py-2">
                {t.had_error ? (
                  <StatusBadge tone="err">error</StatusBadge>
                ) : (
                  <StatusBadge tone="ok">ok</StatusBadge>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
