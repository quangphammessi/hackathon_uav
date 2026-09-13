"use client";

import { useEffect, useRef, useState } from "react";
import { eventStreamUrl, getEvents, type EventEnvelope } from "@/lib/api";

const MAX_EVENTS = 50;
const POLL_INTERVAL_MS = 3000;

function topicTone(topic: string): string {
  if (topic.startsWith("pricing.")) return "text-accent";
  if (topic.startsWith("trust.")) return "text-ok";
  if (topic.startsWith("storefront.")) return "text-warn";
  if (topic.startsWith("payments.")) return "text-err";
  if (topic.startsWith("observability.")) return "text-text-faint";
  return "text-text-dim";
}

export function EventFeed() {
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [live, setLive] = useState(false);
  const seenIds = useRef<Set<string>>(new Set());

  useEffect(() => {
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    function addEvent(e: EventEnvelope) {
      if (seenIds.current.has(e.event_id)) return;
      seenIds.current.add(e.event_id);
      setEvents((prev) => [e, ...prev].slice(0, MAX_EVENTS));
    }

    function startPolling() {
      if (pollTimer || cancelled) return;
      setLive(false);
      const poll = () => {
        getEvents(MAX_EVENTS)
          .then((resp) => {
            for (const e of [...resp.events].reverse()) {
              if (e.topic === "connection.open") continue;
              addEvent(e);
            }
          })
          .catch(() => {
            // gateway unreachable -- keep retrying silently
          });
      };
      poll();
      pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    }

    try {
      es = new EventSource(eventStreamUrl());
      es.onopen = () => {
        if (!cancelled) setLive(true);
      };
      es.onmessage = (msg) => {
        try {
          const payload = JSON.parse(msg.data) as EventEnvelope | { topic: string };
          if (payload.topic === "connection.open") return;
          addEvent(payload as EventEnvelope);
        } catch {
          // ignore malformed frame
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      cancelled = true;
      es?.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, []);

  return (
    <div className="rounded-lg border border-border bg-bg-panel">
      <div className="flex items-center justify-between border-b border-border-soft px-4 py-2.5">
        <h3 className="text-sm font-semibold text-text-dim">Live event feed</h3>
        <span
          className={`flex items-center gap-1.5 text-[10px] ${
            live ? "text-ok" : "text-text-faint"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${live ? "bg-ok" : "bg-text-faint"}`}
          />
          {live ? "streaming" : "polling"}
        </span>
      </div>
      <div className="max-h-[360px] space-y-1 overflow-y-auto p-3 scrollbar-thin">
        {events.length === 0 && (
          <p className="px-1 py-4 text-center text-xs text-text-faint">
            Waiting for events…
          </p>
        )}
        {events.map((e) => (
          <div
            key={e.event_id}
            className="flex items-start gap-2 rounded-md px-2 py-1.5 font-mono text-[11px] hover:bg-bg-raised"
          >
            <span className="shrink-0 text-text-faint">
              {new Date(e.ts * 1000).toLocaleTimeString()}
            </span>
            <span className={`shrink-0 font-medium ${topicTone(e.topic)}`}>
              {e.topic}
            </span>
            <span className="min-w-0 truncate text-text-faint">
              {JSON.stringify(e.payload)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
