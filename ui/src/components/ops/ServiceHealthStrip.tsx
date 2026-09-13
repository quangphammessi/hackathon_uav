"use client";

import { useState } from "react";
import type { ServiceStatus } from "@/lib/api";
import { Dot, StatusBadge } from "@/components/StatusBadge";

export function ServiceHealthStrip({
  services,
}: {
  services: Record<string, ServiceStatus>;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const entries = Object.entries(services);

  if (entries.length === 0) {
    return (
      <p className="text-sm text-text-faint">No service status reported.</p>
    );
  }

  return (
    <div className="flex flex-wrap gap-2">
      {entries.map(([name, status]) => {
        const ok = status.status === "ok";
        const isOpen = expanded === name;
        return (
          <div key={name} className="relative">
            <button
              type="button"
              onClick={() => setExpanded(isOpen ? null : name)}
              className={`flex items-center gap-2 rounded-md border px-3 py-2 text-xs transition-colors ${
                ok
                  ? "border-ok/30 bg-ok/5 hover:bg-ok/10"
                  : "border-warn/30 bg-warn/5 hover:bg-warn/10"
              }`}
            >
              <Dot tone={ok ? "ok" : "warn"} />
              <span className="font-medium">{name}</span>
              <span className="text-text-faint">v{status.version}</span>
            </button>
            {isOpen && (
              <div className="absolute left-0 z-20 mt-2 w-80 rounded-lg border border-border bg-bg-raised p-3 shadow-xl">
                <div className="mb-2 flex items-center justify-between">
                  <StatusBadge tone={ok ? "ok" : "warn"}>
                    {status.status}
                  </StatusBadge>
                  <span className="text-[10px] text-text-faint">
                    {status.environment}
                  </span>
                </div>
                {status.detail && (
                  <p className="mb-2 text-xs text-text-dim">{status.detail}</p>
                )}
                <pre className="max-h-56 overflow-auto rounded-md border border-border-soft bg-bg p-2 font-mono text-[10px] leading-relaxed text-text-dim scrollbar-thin">
                  {JSON.stringify(status.backends, null, 2)}
                </pre>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
