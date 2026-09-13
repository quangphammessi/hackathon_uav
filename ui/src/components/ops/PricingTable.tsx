import type { MarketSkuSummary } from "@/lib/api";

export function PricingTable({
  skus,
  selectedSku,
  onSelect,
}: {
  skus: MarketSkuSummary[];
  selectedSku: string | null;
  onSelect: (sku: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[640px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">SKU</th>
            <th className="px-3 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">List</th>
            <th className="px-3 py-2 font-medium">MAP</th>
            <th className="px-3 py-2 font-medium">Inventory</th>
            <th className="px-3 py-2 font-medium">Competitors</th>
          </tr>
        </thead>
        <tbody>
          {skus.map((s) => (
            <tr
              key={s.sku}
              onClick={() => onSelect(s.sku)}
              className={`cursor-pointer border-b border-border-soft last:border-0 transition-colors hover:bg-bg-raised ${
                selectedSku === s.sku ? "bg-accent/5" : ""
              }`}
            >
              <td className="px-3 py-2 font-mono text-text-dim">{s.sku}</td>
              <td className="px-3 py-2">{s.name}</td>
              <td className="px-3 py-2 font-mono">{s.list_price.toFixed(2)}</td>
              <td className="px-3 py-2 font-mono">{s.map_price.toFixed(2)}</td>
              <td className="px-3 py-2 font-mono">{s.inventory_units}</td>
              <td className="px-3 py-2 font-mono text-text-dim">
                {s.competitor_count} ({s.competitor_min.toFixed(2)}&ndash;
                {s.competitor_max.toFixed(2)})
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
