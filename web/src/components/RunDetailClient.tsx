"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Check,
  Clock3,
  FileJson,
  GitPullRequest,
  Layers,
  ShieldCheck,
  TerminalSquare,
  X,
} from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import AgentGraph from "@/components/AgentGraph";
import {
  CodeBlock,
  CompactList,
  CompactRow,
  EmptyState,
  MetricTile,
  PageHeader,
  PageTabs,
  Panel,
  StatusBadge,
} from "@/components/ui/primitives";
import {
  decideApproval,
  getRun,
  getRunMetrics,
  getRuntimeStatus,
  getSystemMetrics,
  listAgents,
  listArtifacts,
  listRunApprovals,
  listToolAudit,
  resumeRun,
  stopRun,
} from "@/lib/api";
import {
  asPercent,
  formatBytes,
  formatDateTime,
  formatUnknown,
  nestedString,
  nestedUnknown,
  shortId,
} from "@/lib/format";
import { useRunEvents } from "@/hooks/useRunEvents";
import type { Approval, Artifact, Run, RunEvent, RunMetrics, RuntimeStatus, SystemMetrics, ToolAudit } from "@/types";

type RunTab = "overview" | "agents" | "timeline" | "artifacts" | "diff-tests" | "approvals" | "metrics";

type EventGroup = {
  key: string;
  role: string;
  events: RunEvent[];
};

const RUN_TABS: Array<{
  id: RunTab;
  label: string;
  description: string;
  icon: typeof Layers;
}> = [
  { id: "overview", label: "Overview", description: "result", icon: Layers },
  { id: "agents", label: "Agents", description: "graph", icon: Bot },
  { id: "timeline", label: "Timeline", description: "events", icon: Clock3 },
  { id: "artifacts", label: "Artifacts", description: "outputs", icon: FileJson },
  { id: "diff-tests", label: "Diff / Tests", description: "coding", icon: GitPullRequest },
  { id: "approvals", label: "Approvals", description: "gates", icon: ShieldCheck },
  { id: "metrics", label: "Metrics", description: "usage", icon: Activity },
];
const RUN_ACTIVE_STATUSES = new Set(["created", "queued", "running", "waiting_approval", "cancelling"]);

export default function RunDetailClient({ runId }: { runId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab") as RunTab | null;
  const activeTab = RUN_TABS.some((tab) => tab.id === tabParam) ? tabParam ?? "overview" : "overview";

  const runQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => getRun(runId),
    refetchInterval: 4000,
  });
  const artifactsQuery = useQuery({
    queryKey: ["artifacts", runId],
    queryFn: () => listArtifacts(runId),
  });
  const auditQuery = useQuery({
    queryKey: ["tool-audit", runId],
    queryFn: () => listToolAudit(runId),
  });
  const approvalsQuery = useQuery({
    queryKey: ["approvals", runId],
    queryFn: () => listRunApprovals(runId),
  });
  const metricsQuery = useQuery({
    queryKey: ["run-metrics", runId],
    queryFn: () => getRunMetrics(runId),
    refetchInterval: 4000,
  });
  const agentsQuery = useQuery({
    queryKey: ["agents"],
    queryFn: listAgents,
  });
  const systemMetricsQuery = useQuery({
    queryKey: ["system-metrics"],
    queryFn: getSystemMetrics,
    refetchInterval: 4000,
  });
  const runtimeStatusQuery = useQuery({
    queryKey: ["runtime-status"],
    queryFn: getRuntimeStatus,
    refetchInterval: 4000,
  });
  const events = useRunEvents(runId);

  const setTab = (tab: RunTab) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set("tab", tab);
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  };

  const run = runQuery.data ?? null;
  const artifacts = artifactsQuery.data ?? [];
  const audit = auditQuery.data ?? [];
  const approvals = approvalsQuery.data ?? [];
  const metrics = metricsQuery.data ?? null;
  const system = systemMetricsQuery.data ?? null;
  const runtime = runtimeStatusQuery.data ?? null;
  const stopMutation = useMutation({
    mutationFn: () => stopRun(runId),
    onSuccess: () => {
      void runQuery.refetch();
      void metricsQuery.refetch();
      void approvalsQuery.refetch();
    },
  });

  const tabItems = RUN_TABS.map((tab) => ({
    ...tab,
    count:
      tab.id === "timeline"
        ? events.length
        : tab.id === "artifacts"
          ? artifacts.length
          : tab.id === "approvals"
            ? approvals.filter((approval) => approval.status === "pending").length
            : undefined,
  }));

  if (runQuery.isLoading && !run) {
    return <EmptyState title="Loading run" text={runId} />;
  }

  if (runQuery.error) {
    return <EmptyState title="Run load failed" text={runQuery.error.message} />;
  }

  if (!run) {
    return <EmptyState title="Run not found" text={runId} />;
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow={`run ${shortId(run.id)}`}
        title={run.task}
        description={run.workspace ?? "No workspace configured"}
        icon={TerminalSquare}
        summary={
          <RunSummary
            run={run}
            metrics={metrics}
            onStop={RUN_ACTIVE_STATUSES.has(run.status) ? () => stopMutation.mutate() : undefined}
            stopping={stopMutation.isPending}
          />
        }
      />
      {stopMutation.error ? <div className="error-line">{stopMutation.error.message}</div> : null}
      <PageTabs active={activeTab} items={tabItems} onChange={setTab} ariaLabel="Run detail tabs" />
      {activeTab === "overview" ? (
        <OverviewTab run={run} events={events} artifacts={artifacts} approvals={approvals} metrics={metrics} runtime={runtime} />
      ) : null}
      {activeTab === "agents" ? (
        <Panel title="Agent graph" className="full-height-panel">
          <AgentGraph run={run} events={events} agents={agentsQuery.data ?? []} />
        </Panel>
      ) : null}
      {activeTab === "timeline" ? <TimelineTab events={events} /> : null}
      {activeTab === "artifacts" ? <ArtifactsTab artifacts={artifacts} /> : null}
      {activeTab === "diff-tests" ? <DiffTestsTab audit={audit} /> : null}
      {activeTab === "approvals" ? <ApprovalsTab approvals={approvals} runId={runId} /> : null}
      {activeTab === "metrics" ? <MetricsTab metrics={metrics} system={system} runtime={runtime} /> : null}
    </div>
  );
}

function RunSummary({
  run,
  metrics,
  onStop,
  stopping,
}: {
  run: Run;
  metrics: RunMetrics | null;
  onStop?: () => void;
  stopping?: boolean;
}) {
  return (
    <div className="summary-grid">
      <MetricTile label="Status" value={<StatusBadge value={run.status} />} />
      <MetricTile label="Mode" value={run.mode} />
      <MetricTile label="Provider" value={run.model_provider} />
      <MetricTile label="Events" value={metrics?.event_count ?? 0} />
      <MetricTile label="Tokens" value={metrics?.token_usage.total_tokens ?? "n/a"} />
      <MetricTile
        label={onStop ? "Control" : "Updated"}
        value={
          onStop ? (
            <button className="secondary-button danger-button compact-control" type="button" onClick={onStop} disabled={stopping}>
              <X size={14} aria-hidden />
              {stopping ? "Stopping" : "Stop"}
            </button>
          ) : (
            formatDateTime(run.updated_at)
          )
        }
      />
    </div>
  );
}

function OverviewTab({
  run,
  events,
  artifacts,
  approvals,
  metrics,
  runtime,
}: {
  run: Run;
  events: RunEvent[];
  artifacts: Artifact[];
  approvals: Approval[];
  metrics: RunMetrics | null;
  runtime: RuntimeStatus | null;
}) {
  const selectedRoles = Array.from(
    new Set(events.map((event) => event.role).filter((role): role is string => role !== null)),
  ).sort();
  const latestEventGroups = groupAdjacentEvents(events.slice(-6).reverse());
  return (
    <div className="overview-grid">
      <Panel title="Final synthesis" className="overview-main">
        {run.final_answer ? <CodeBlock value={run.final_answer} className="answer-block" /> : <EmptyState title="No final answer yet" />}
      </Panel>
      <Panel title="Run pulse">
        <div className="metric-list">
          <MetricTile label="Model calls" value={metrics?.model_call_count ?? 0} />
          <MetricTile label="Tool calls" value={metrics?.tool_call_count ?? 0} />
          <MetricTile label="Pending approvals" value={metrics?.pending_approval_count ?? approvals.filter((item) => item.status === "pending").length} />
          <MetricTile label="Artifacts" value={artifacts.length} />
        </div>
      </Panel>
      <Panel title="Diagnostics">
        <div className="metric-list">
          <MetricTile label="Queue" value={runtime?.queue_depth ?? 0} />
          <MetricTile label="Worker" value={run.worker_id ? shortId(run.worker_id) : "unclaimed"} />
          <MetricTile label="Heartbeat" value={run.heartbeat_at ? formatDateTime(run.heartbeat_at) : "n/a"} />
          <MetricTile
            label="Sandbox"
            value={<StatusBadge value={runtime?.sandbox.available ? "ready" : "error"}>{runtime?.sandbox.backend ?? "unknown"}</StatusBadge>}
            tone={runtime?.sandbox.available === false ? "danger" : "normal"}
          />
        </div>
        {run.error ? <div className="error-line">{run.error}</div> : null}
        {runtime?.stale_running_count ? <div className="error-line">Stale running runs: {runtime.stale_running_count}</div> : null}
      </Panel>
      <Panel title="Roles">
        <div className="chip-list">
          {selectedRoles.length ? selectedRoles.map((role) => <StatusBadge key={role} value={role} />) : <span className="muted">No roles selected yet</span>}
        </div>
      </Panel>
      <Panel title="Latest events">
        <CompactList>
          {latestEventGroups.map((group) => (
            <div key={group.key} className="event-role-group">
              <div className="event-role-header">
                <StatusBadge value={group.role} />
              </div>
              {group.events.map((event) => (
                <CompactRow key={event.id} className="event-row-compact compact">
                  <span className="mono">#{event.id}</span>
                  <strong>{event.event_type}</strong>
                </CompactRow>
              ))}
            </div>
          ))}
        </CompactList>
      </Panel>
    </div>
  );
}

function TimelineTab({ events }: { events: RunEvent[] }) {
  const [typeFilter, setTypeFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const eventTypes = useMemo(() => Array.from(new Set(events.map((event) => event.event_type))).sort(), [events]);
  const roles = useMemo(() => Array.from(new Set(events.map((event) => event.role ?? "system"))).sort(), [events]);
  const filtered = events.filter((event) => {
    const typeOk = typeFilter === "all" || event.event_type === typeFilter;
    const roleOk = roleFilter === "all" || (event.role ?? "system") === roleFilter;
    return typeOk && roleOk;
  });
  const grouped = groupAdjacentEvents(filtered);
  const selected = filtered.find((event) => event.id === selectedId) ?? filtered.at(-1) ?? null;

  return (
    <div className="detail-split timeline-layout">
      <Panel
        title="Timeline"
        action={
          <div className="filter-row">
            <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} aria-label="Event type">
              <option value="all">all types</option>
              {eventTypes.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value)} aria-label="Role">
              <option value="all">all roles</option>
              {roles.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <CompactList className="timeline-list">
          {grouped.map((group) => (
            <div key={group.key} className="event-role-group">
              <div className="event-role-header">
                <StatusBadge value={group.role} />
              </div>
              {group.events.map((event) => (
                <button key={event.id} className="event-button" type="button" onClick={() => setSelectedId(event.id)}>
                  <CompactRow selected={selected?.id === event.id} className="timeline-row compact">
                    <span className="mono">#{event.id}</span>
                    <span>
                      <strong>{event.event_type}</strong>
                      <em>{formatDateTime(event.created_at)}</em>
                    </span>
                  </CompactRow>
                </button>
              ))}
            </div>
          ))}
        </CompactList>
      </Panel>
      <Panel title="Event payload" className="payload-panel">
        {selected ? (
          <CodeBlock value={JSON.stringify(selected, null, 2)} className="large-code" />
        ) : (
          <EmptyState title="No events" />
        )}
      </Panel>
    </div>
  );
}

function groupAdjacentEvents(events: RunEvent[]): EventGroup[] {
  const groups: EventGroup[] = [];
  for (const event of events) {
    const role = event.role ?? "system";
    const previous = groups.at(-1);
    if (previous?.role === role) {
      previous.events.push(event);
      continue;
    }
    groups.push({ key: `${role}-${event.id}`, role, events: [event] });
  }
  return groups;
}

function ArtifactsTab({ artifacts }: { artifacts: Artifact[] }) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const active = artifacts.find((artifact) => artifact.id === activeId) ?? artifacts[0] ?? null;

  useEffect(() => {
    if (artifacts.length && !artifacts.some((artifact) => artifact.id === activeId)) {
      setActiveId(artifacts[0].id);
    }
  }, [activeId, artifacts]);

  return (
    <div className="detail-split artifacts-layout">
      <Panel title="Artifacts">
        <CompactList className="artifact-list">
          {artifacts.map((artifact) => (
            <button key={artifact.id} type="button" className="event-button" onClick={() => setActiveId(artifact.id)}>
              <CompactRow selected={artifact.id === active?.id} className="artifact-row">
                <StatusBadge value={artifact.kind} />
                <strong>{artifact.path ?? shortId(artifact.id)}</strong>
                <em>{formatDateTime(artifact.created_at)}</em>
              </CompactRow>
            </button>
          ))}
          {!artifacts.length ? <EmptyState title="No artifacts" /> : null}
        </CompactList>
      </Panel>
      <Panel title={active ? `Artifact: ${active.kind}` : "Artifact content"} className="payload-panel">
        {active ? (
          <CodeBlock value={JSON.stringify(active.content, null, 2)} className="large-code" />
        ) : (
          <EmptyState title="No artifact selected" />
        )}
      </Panel>
    </div>
  );
}

function DiffTestsTab({ audit }: { audit: ToolAudit[] }) {
  const diff = audit
    .filter((record) => record.tool_name === "native.git_diff")
    .map((record) => nestedString(record.output, ["output", "stdout"]))
    .filter(Boolean)
    .at(-1);
  const verification = audit
    .filter((record) => record.tool_name === "native.verify")
    .map((record) => nestedUnknown(record.output, ["output", "commands"]))
    .at(-1);

  return (
    <div className="coding-workbench">
      <Panel title="Diff" className="payload-panel">
        <CodeBlock value={diff || "No diff"} className="large-code diff-code" />
      </Panel>
      <Panel title="Tests" className="payload-panel">
        <CodeBlock value={formatUnknown(verification) || "No test output"} className="large-code" />
      </Panel>
      <Panel title="Tool audit" className="tool-audit-panel">
        <CompactList>
          {audit.map((record) => (
            <CompactRow key={record.id} className="audit-row">
              <span className="mono">#{record.id}</span>
              <strong>{record.tool_name}</strong>
              <StatusBadge value={record.status} />
              <em>{record.role}</em>
            </CompactRow>
          ))}
        </CompactList>
      </Panel>
    </div>
  );
}

function ApprovalsTab({ approvals, runId }: { approvals: Approval[]; runId: string }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async ({
      approvalId,
      decision,
    }: {
      approvalId: string;
      decision: "approve" | "reject";
    }) => {
      await decideApproval(approvalId, decision, `${decision} from Synode UI`);
      if (decision === "approve") {
        await resumeRun(runId);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["approvals", runId] });
      void queryClient.invalidateQueries({ queryKey: ["run", runId] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["run-metrics", runId] });
    },
  });

  return (
    <Panel title="Approvals">
      <CompactList>
        {approvals.map((approval) => (
          <CompactRow key={approval.id} className="approval-row">
            <div>
              <strong>{approval.tool_name}</strong>
              <em>{approval.reason}</em>
            </div>
            <StatusBadge value={approval.status} />
            {approval.status === "pending" ? (
              <div className="approval-actions">
                <button
                  className="icon-button approve"
                  title="Approve"
                  type="button"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate({ approvalId: approval.id, decision: "approve" })}
                >
                  <Check size={16} aria-hidden />
                </button>
                <button
                  className="icon-button reject"
                  title="Reject"
                  type="button"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate({ approvalId: approval.id, decision: "reject" })}
                >
                  <X size={16} aria-hidden />
                </button>
              </div>
            ) : (
              <span className="muted">{approval.decision_reason ?? "decided"}</span>
            )}
          </CompactRow>
        ))}
        {!approvals.length ? <EmptyState title="No approvals" /> : null}
      </CompactList>
      {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
    </Panel>
  );
}

function MetricsTab({
  metrics,
  system,
  runtime,
}: {
  metrics: RunMetrics | null;
  system: SystemMetrics | null;
  runtime: RuntimeStatus | null;
}) {
  return (
    <div className="metrics-workbench">
      <Panel title="Run metrics">
        <div className="summary-grid">
          <MetricTile label="Duration" value={metrics?.duration_ms ? `${metrics.duration_ms} ms` : "n/a"} />
          <MetricTile label="Events" value={metrics?.event_count ?? 0} />
          <MetricTile label="Model calls" value={metrics?.model_call_count ?? 0} />
          <MetricTile label="Tool calls" value={metrics?.tool_call_count ?? 0} />
          <MetricTile label="Failed tools" value={metrics?.failed_tool_call_count ?? 0} tone={(metrics?.failed_tool_call_count ?? 0) > 0 ? "danger" : "normal"} />
          <MetricTile label="Tokens" value={metrics?.token_usage.total_tokens ?? "n/a"} />
        </div>
      </Panel>
      <Panel title="Provider usage">
        <CompactList>
          {Object.entries(metrics?.provider_usage ?? {}).map(([provider, usage]) => (
            <CompactRow key={provider} className="provider-row">
              <strong>{provider}</strong>
              <span>input {usage.input_tokens ?? "n/a"}</span>
              <span>output {usage.output_tokens ?? "n/a"}</span>
              <span>total {usage.total_tokens ?? "n/a"}</span>
            </CompactRow>
          ))}
          {!Object.keys(metrics?.provider_usage ?? {}).length ? <EmptyState title="No provider usage" /> : null}
        </CompactList>
      </Panel>
      <Panel title="System resources">
        <div className="summary-grid">
          <MetricTile label="CPU" value={asPercent(system?.process.cpu_percent)} />
          <MetricTile label="Memory RSS" value={formatBytes(system?.process.memory_rss_bytes)} />
          <MetricTile label="Memory %" value={asPercent(system?.process.memory_percent)} />
          <MetricTile label="Uptime" value={system ? `${Math.round(system.process.uptime_seconds)}s` : "n/a"} />
        </div>
        <div className="resource-bars">
          <ResourceBar label="CPU" value={system?.process.cpu_percent ?? 0} />
          <ResourceBar label="RAM" value={system?.process.memory_percent ?? 0} />
          <ResourceBar label="GPU" value={system?.gpu[0]?.utilization_percent ?? 0} />
        </div>
      </Panel>
      <Panel title="Worker queue">
        <div className="summary-grid">
          <MetricTile label="Queued" value={runtime?.queue_depth ?? 0} />
          <MetricTile label="Running" value={runtime?.running_count ?? 0} />
          <MetricTile label="Cancelling" value={runtime?.cancelling_count ?? 0} />
          <MetricTile label="Stale" value={runtime?.stale_running_count ?? 0} tone={(runtime?.stale_running_count ?? 0) > 0 ? "danger" : "normal"} />
          <MetricTile
            label="OpenHands"
            value={<StatusBadge value={runtime?.execution_backends.openhands?.available ? "ready" : "disabled"} />}
            tone={runtime?.execution_backends.openhands?.available === false ? "danger" : "normal"}
          />
        </div>
        <CompactList>
          {(runtime?.workers ?? []).map((worker) => (
            <CompactRow key={worker.worker_id} className="provider-row">
              <strong>{shortId(worker.worker_id)}</strong>
              <StatusBadge value={worker.status} />
              <span>{worker.current_run_id ? shortId(worker.current_run_id) : "idle"}</span>
              <em>{formatDateTime(worker.heartbeat_at)}</em>
            </CompactRow>
          ))}
          {runtime && !runtime.workers.length ? <EmptyState title="No worker heartbeat" /> : null}
        </CompactList>
      </Panel>
    </div>
  );
}

function ResourceBar({ label, value }: { label: string; value: number }) {
  const width = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
  return (
    <div className="resource-bar">
      <span>{label}</span>
      <em>{width.toFixed(1)}%</em>
      <div>
        <strong style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}
