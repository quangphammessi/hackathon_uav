import type { OrderSummary } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export function OrdersTable({ orders }: { orders: OrderSummary[] }) {
  if (orders.length === 0) {
    return <p className="text-sm text-text-faint">No orders recorded yet.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[760px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">Order</th>
            <th className="px-3 py-2 font-medium">SKU</th>
            <th className="px-3 py-2 font-medium">Amount</th>
            <th className="px-3 py-2 font-medium">Rail</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Agent</th>
            <th className="px-3 py-2 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.order_id} className="border-b border-border-soft last:border-0">
              <td className="px-3 py-2 font-mono text-text-dim">{o.order_id}</td>
              <td className="px-3 py-2 font-mono text-text-dim">{o.sku}</td>
              <td className="px-3 py-2 font-mono">
                {o.amount.toFixed(2)} {o.currency}
              </td>
              <td className="px-3 py-2 font-mono text-text-dim">{o.rail ?? "--"}</td>
              <td className="px-3 py-2">
                <StatusBadge tone={o.status === "SETTLED" ? "ok" : "err"}>
                  {o.status}
                </StatusBadge>
                {o.reason_code && (
                  <span className="ml-1.5 font-mono text-[10px] text-text-faint">
                    {o.reason_code}
                  </span>
                )}
              </td>
              <td className="px-3 py-2 font-mono text-text-dim">{o.agent_id}</td>
              <td className="px-3 py-2 font-mono text-text-faint">
                {formatTimestamp(o.created_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatTimestamp(raw: string): string {
  const n = Number(raw);
  if (Number.isNaN(n)) return raw;
  return new Date(n * 1000).toLocaleTimeString();
}
