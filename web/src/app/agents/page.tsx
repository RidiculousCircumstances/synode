"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, GitBranch } from "lucide-react";

import AgentGraph from "@/components/AgentGraph";
import { CompactList, CompactRow, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import { useRunEvents } from "@/hooks/useRunEvents";
import { getRun, listAgents, listRuns } from "@/lib/api";

export default function AgentsPage() {
  const agentsQuery = useQuery({ queryKey: ["agents"], queryFn: listAgents });
  const runsQuery = useQuery({ queryKey: ["runs"], queryFn: listRuns, refetchInterval: 5000 });
  const latestRunId = runsQuery.data?.[0]?.id ?? null;
  const runQuery = useQuery({
    queryKey: ["run", latestRunId],
    queryFn: () => getRun(latestRunId ?? ""),
    enabled: latestRunId !== null,
    refetchInterval: 4000,
  });
  const events = useRunEvents(latestRunId);
  const agents = agentsQuery.data ?? [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="nodes"
        title="Agents"
        description="Role inventory and latest run graph."
        icon={Bot}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Roles" value={agents.length} />
            <MetricTile label="Latest run" value={latestRunId ? latestRunId.slice(0, 8) : "n/a"} icon={GitBranch} />
          </div>
        }
      />
      <div className="agents-layout">
        <Panel title="Latest run graph" className="full-height-panel">
          <AgentGraph run={runQuery.data ?? null} events={events} agents={agents} />
        </Panel>
        <Panel title="Role catalog">
          <CompactList>
            {agents.map((agent) => (
              <CompactRow key={agent.name} className="agent-catalog-row">
                <StatusBadge value={agent.name} />
                <strong>{agent.mission}</strong>
                <em>{agent.allowed_tools.length} tools</em>
              </CompactRow>
            ))}
          </CompactList>
          {agentsQuery.error ? <div className="error-line">{agentsQuery.error.message}</div> : null}
        </Panel>
      </div>
    </div>
  );
}
