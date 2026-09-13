import { Suspense } from "react";
import { AgentConsole } from "@/components/AgentConsole";

export default function Home() {
  return (
    <Suspense
      fallback={<div className="text-sm text-text-dim">Loading console…</div>}
    >
      <AgentConsole />
    </Suspense>
  );
}
