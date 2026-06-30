"use client";

import { useQuery } from "@tanstack/react-query";
import { MessageSquare, Play, Workflow } from "lucide-react";

import RunComposer from "@/components/RunComposer";
import RunsTable from "@/components/RunsTable";
import { MetricTile, PageHeader, Panel } from "@/components/ui/primitives";
import { listRuns } from "@/lib/api";

export default function ChatPage() {
  const runsQuery = useQuery({
    queryKey: ["runs"],
    queryFn: listRuns,
    refetchInterval: 5000,
  });
  const runs = runsQuery.data ?? [];
  const running = runs.filter((run) => run.status === "running").length;
  const waiting = runs.filter((run) => run.status === "waiting_approval").length;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="workspace"
        title="Chat"
        description="Create a multi-agent run and follow it in the dedicated run workspace."
        icon={MessageSquare}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Runs" value={runs.length} icon={Workflow} />
            <MetricTile label="Running" value={running} icon={Play} />
            <MetricTile label="Approvals" value={waiting} />
          </div>
        }
      />
      <div className="chat-layout">
        <Panel title="New run" className="composer-panel">
          <RunComposer />
        </Panel>
        <Panel title="Recent runs">
          {runsQuery.error ? <div className="error-line">{runsQuery.error.message}</div> : null}
          <RunsTable runs={runs.slice(0, 8)} />
        </Panel>
      </div>
    </div>
  );
}
