import type { ProvenanceSkuSummary } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export function ProvenanceTable({
  skus,
  selectedSku,
  onSelect,
}: {
  skus: ProvenanceSkuSummary[];
  selectedSku?: string | null;
  onSelect?: (sku: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[720px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">SKU</th>
            <th className="px-3 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">Batch</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Present steps</th>
            <th className="px-3 py-2 font-medium">Missing steps</th>
            <th className="px-3 py-2 font-medium">Chain hash</th>
          </tr>
        </thead>
        <tbody>
          {skus.map((s) => (
            <tr
              key={s.sku}
              onClick={onSelect ? () => onSelect(s.sku) : undefined}
              className={`border-b border-border-soft last:border-0 ${
                !s.ready ? "bg-err/5" : ""
              } ${onSelect ? "cursor-pointer hover:bg-bg-raised" : ""} ${
                selectedSku === s.sku ? "bg-accent/5" : ""
              }`}
            >
              <td className="px-3 py-2 font-mono text-text-dim">{s.sku}</td>
              <td className="px-3 py-2">{s.name}</td>
              <td className="px-3 py-2 font-mono text-text-dim">{s.batch}</td>
              <td className="px-3 py-2">
                <StatusBadge tone={s.ready ? "ok" : "err"}>
                  {s.ready ? "READY" : "CHAIN GAP"}
                </StatusBadge>
              </td>
              <td className="px-3 py-2 font-mono text-text-dim">
                {s.present_steps.join(", ")}
              </td>
              <td className="px-3 py-2 font-mono">
                {s.missing_steps.length > 0 ? (
                  <span className="text-err">{s.missing_steps.join(", ")}</span>
                ) : (
                  <span className="text-text-faint">--</span>
                )}
              </td>
              <td className="px-3 py-2 font-mono text-text-faint">
                {s.event_chain_hash.slice(0, 10)}&hellip;
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
