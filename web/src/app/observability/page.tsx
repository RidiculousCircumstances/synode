"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, Cpu, Gauge, HardDrive } from "lucide-react";

import {
  CompactList,
  CompactRow,
  MetricTile,
  PageHeader,
  Panel,
  StatusBadge,
} from "@/components/ui/primitives";
import { getModelHealth, getSystemMetrics, listRuns } from "@/lib/api";
import { asPercent, formatBytes, formatDateTime } from "@/lib/format";

export default function ObservabilityPage() {
  const modelsQuery = useQuery({ queryKey: ["model-health"], queryFn: getModelHealth, refetchInterval: 10000 });
  const systemQuery = useQuery({ queryKey: ["system-metrics"], queryFn: getSystemMetrics, refetchInterval: 4000 });
  const runsQuery = useQuery({ queryKey: ["runs"], queryFn: listRuns, refetchInterval: 5000 });
  const system = systemQuery.data ?? null;
  const runs = runsQuery.data ?? [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="telemetry"
        title="Observability"
        description="Provider health, process resources, and recent runtime activity."
        icon={Activity}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="CPU" value={asPercent(system?.process.cpu_percent)} icon={Cpu} />
            <MetricTile label="RAM" value={formatBytes(system?.process.memory_rss_bytes)} icon={HardDrive} />
            <MetricTile label="Runs" value={runs.length} icon={Gauge} />
          </div>
        }
      />
      <div className="observability-grid">
        <Panel title="Model providers">
          <CompactList>
            {(modelsQuery.data ?? []).map((model) => (
              <CompactRow key={model.provider} className="provider-row">
                <strong>{model.provider}</strong>
                <StatusBadge value={model.ok ? "ok" : "error"}>{model.ok ? "ok" : "error"}</StatusBadge>
                <span>{model.model ?? "model not reported"}</span>
                <em>{model.error ?? ""}</em>
              </CompactRow>
            ))}
          </CompactList>
          {modelsQuery.error ? <div className="error-line">{modelsQuery.error.message}</div> : null}
        </Panel>
        <Panel title="System process">
          <div className="summary-grid">
            <MetricTile label="PID" value={system?.process.pid ?? "n/a"} />
            <MetricTile label="Uptime" value={system ? `${Math.round(system.process.uptime_seconds)}s` : "n/a"} />
            <MetricTile label="CPU" value={asPercent(system?.process.cpu_percent)} />
            <MetricTile label="Memory" value={formatBytes(system?.process.memory_rss_bytes)} />
          </div>
        </Panel>
        <Panel title="Recent runs">
          <CompactList>
            {runs.slice(0, 8).map((run) => (
              <CompactRow key={run.id} className="recent-run-row">
                <StatusBadge value={run.status} />
                <strong>{run.task}</strong>
                <em>{formatDateTime(run.updated_at)}</em>
              </CompactRow>
            ))}
          </CompactList>
        </Panel>
      </div>
    </div>
  );
}
