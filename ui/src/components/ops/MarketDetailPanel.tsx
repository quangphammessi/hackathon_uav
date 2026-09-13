"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MarketDetail } from "@/lib/api";

export function MarketDetailPanel({
  detail,
  loading,
  error,
}: {
  detail: MarketDetail | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading) {
    return <p className="text-sm text-text-faint">Loading market detail…</p>;
  }
  if (error) {
    return <p className="text-sm text-err">{error}</p>;
  }
  if (!detail) {
    return (
      <p className="text-sm text-text-faint">
        Select a SKU from the pricing table to see competitor pricing and
        bandit performance.
      </p>
    );
  }

  const chartData = detail.competitors.map((c) => ({
    name: c.competitor,
    price: c.price,
  }));

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold">{detail.name}</h3>
        <p className="font-mono text-xs text-text-faint">{detail.sku}</p>
      </div>

      <div className="h-[260px] w-full rounded-lg border border-border bg-bg-panel p-3">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="var(--border-soft)" vertical={false} />
            <XAxis
              dataKey="name"
              tick={{ fill: "var(--text-faint)", fontSize: 11 }}
              stroke="var(--border)"
            />
            <YAxis
              tick={{ fill: "var(--text-faint)", fontSize: 11 }}
              stroke="var(--border)"
              domain={["dataMin - 10", "dataMax + 10"]}
            />
            <Tooltip
              contentStyle={{
                background: "var(--bg-raised)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: "var(--text-dim)" }}
              formatter={(value) =>
                typeof value === "number" ? value.toFixed(2) : String(value ?? "")
              }
            />
            <Bar dataKey="price" fill="var(--accent-strong)" radius={[4, 4, 0, 0]} />
            <ReferenceLine
              y={detail.fair_value}
              stroke="var(--accent)"
              strokeDasharray="4 4"
              label={{ value: "fair value", position: "insideTopRight", fill: "var(--accent)", fontSize: 10 }}
            />
            <ReferenceLine
              y={detail.map_price}
              stroke="var(--warn)"
              strokeDasharray="4 4"
              label={{ value: "MAP", position: "insideBottomRight", fill: "var(--warn)", fontSize: 10 }}
            />
            <ReferenceLine
              y={detail.margin_floor}
              stroke="var(--err)"
              strokeDasharray="4 4"
              label={{ value: "margin floor", position: "insideBottomLeft", fill: "var(--err)", fontSize: 10 }}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-xs text-text-dim sm:grid-cols-4">
        <Stat label="list" value={detail.list_price.toFixed(2)} />
        <Stat label="map" value={detail.map_price.toFixed(2)} />
        <Stat label="fair value" value={detail.fair_value.toFixed(2)} />
        <Stat label="margin floor" value={detail.margin_floor.toFixed(2)} />
      </dl>

      <div>
        <h4 className="mb-2 text-xs font-semibold text-text-dim">
          Pricing bandit arms
        </h4>
        <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
          <table className="w-full min-w-[360px] text-left text-xs">
            <thead>
              <tr className="border-b border-border-soft text-text-faint">
                <th className="px-3 py-2 font-medium">Spread</th>
                <th className="px-3 py-2 font-medium">Pulls</th>
                <th className="px-3 py-2 font-medium">Mean reward</th>
              </tr>
            </thead>
            <tbody>
              {detail.bandit_arms.map((arm) => (
                <tr key={arm.spread} className="border-b border-border-soft last:border-0">
                  <td className="px-3 py-2 font-mono">{(arm.spread * 100).toFixed(0)}%</td>
                  <td className="px-3 py-2 font-mono">{arm.pulls}</td>
                  <td className="px-3 py-2 font-mono">{arm.mean_reward.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-text-faint">{label}</dt>
      <dd className="text-text">{value}</dd>
    </div>
  );
}
