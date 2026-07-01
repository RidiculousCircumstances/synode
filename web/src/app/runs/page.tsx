"use client";

import { useQuery } from "@tanstack/react-query";
import { GitBranch, ListFilter } from "lucide-react";
import { useMemo, useState } from "react";

import RunsTable from "@/components/RunsTable";
import { MetricTile, PageHeader, Panel } from "@/components/ui/primitives";
import { listRuns } from "@/lib/api";
import type { RunStatus } from "@/types";

const STATUS_OPTIONS: Array<"all" | RunStatus> = [
  "all",
  "created",
  "queued",
  "running",
  "waiting_approval",
  "waiting_operator",
  "cancelling",
  "completed",
  "failed",
  "failed_verification",
  "cancelled",
];

export default function RunsPage() {
  const [status, setStatus] = useState<"all" | RunStatus>("all");
  const [query, setQuery] = useState("");
  const runsQuery = useQuery({
    queryKey: ["runs"],
    queryFn: listRuns,
    refetchInterval: 5000,
  });
  const runs = runsQuery.data ?? [];
  const filtered = useMemo(
    () =>
      runs.filter((run) => {
        const statusOk = status === "all" || run.status === status;
        const queryOk = !query.trim() || `${run.task} ${run.workspace ?? ""} ${run.id}`.toLowerCase().includes(query.trim().toLowerCase());
        return statusOk && queryOk;
      }),
    [query, runs, status],
  );

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="runtime"
        title="Runs"
        description="Browse active and completed Synode executions."
        icon={GitBranch}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Total" value={runs.length} />
            <MetricTile label="Queued" value={runs.filter((run) => run.status === "queued").length} />
            <MetricTile label="Running" value={runs.filter((run) => run.status === "running").length} />
            <MetricTile
              label="Blocked"
              value={runs.filter((run) => run.status === "waiting_approval" || run.status === "waiting_operator").length}
            />
          </div>
        }
      />
      <Panel
        title="Run registry"
        action={
          <div className="filter-row">
            <ListFilter size={16} aria-hidden />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search runs" />
            <select value={status} onChange={(event) => setStatus(event.target.value as "all" | RunStatus)}>
              {STATUS_OPTIONS.map((item) => (
                <option key={item} value={item}>
                  {item.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {runsQuery.error ? <div className="error-line">{runsQuery.error.message}</div> : null}
        <RunsTable runs={filtered} />
      </Panel>
    </div>
  );
}
