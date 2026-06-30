"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { eventStreamUrl, listEvents } from "@/lib/api";
import type { RunEvent } from "@/types";

const EVENT_TYPES = [
  "run_created",
  "run_started",
  "intake_completed",
  "node_started",
  "node_completed",
  "role_selected",
  "model_invoked",
  "tool_called",
  "approval_required",
  "approval_decided",
  "artifact_created",
  "verification_completed",
  "run_completed",
  "run_failed",
];

export function useRunEvents(runId: string | null): RunEvent[] {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    sourceRef.current?.close();
    setEvents([]);
    if (!runId) {
      return;
    }

    let disposed = false;
    void listEvents(runId).then(async (initial) => {
      if (disposed) {
        return;
      }
      setEvents(initial);
      const lastId = initial.at(-1)?.id ?? 0;
      const source = new EventSource(await eventStreamUrl(runId, lastId));
      sourceRef.current = source;
      const handler = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as RunEvent;
        setEvents((current) => mergeEvent(current, event));
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        void queryClient.invalidateQueries({ queryKey: ["run", runId] });
        void queryClient.invalidateQueries({ queryKey: ["artifacts", runId] });
        void queryClient.invalidateQueries({ queryKey: ["tool-audit", runId] });
        void queryClient.invalidateQueries({ queryKey: ["approvals", runId] });
        void queryClient.invalidateQueries({ queryKey: ["run-metrics", runId] });
      };
      EVENT_TYPES.forEach((type) => source.addEventListener(type, handler as EventListener));
      source.onerror = () => source.close();
    });

    return () => {
      disposed = true;
      sourceRef.current?.close();
    };
  }, [queryClient, runId]);

  return events;
}

function mergeEvent(events: RunEvent[], next: RunEvent): RunEvent[] {
  if (events.some((event) => event.id === next.id)) {
    return events;
  }
  return [...events, next].sort((left, right) => left.id - right.id);
}
