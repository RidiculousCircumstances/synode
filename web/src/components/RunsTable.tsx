"use client";

import { formatDistanceToNowStrict } from "date-fns";
import { ArrowRight } from "lucide-react";
import Link from "next/link";

import { formatDateTime, shortId } from "@/lib/format";
import type { Run } from "@/types";
import { CompactList, CompactRow, EmptyState, StatusBadge } from "@/components/ui/primitives";

export default function RunsTable({ runs, selectedRunId }: { runs: Run[]; selectedRunId?: string }) {
  if (!runs.length) {
    return <EmptyState title="No runs yet" text="Create a thread to start the first run." />;
  }

  return (
    <CompactList>
      <div className="compact-header runs-grid">
        <span>Run</span>
        <span>Task</span>
        <span>Status</span>
        <span>Mode</span>
        <span>Updated</span>
        <span />
      </div>
      {runs.map((run) => (
        <Link key={run.id} href={`/runs/${run.id}`} className="row-link">
          <CompactRow selected={selectedRunId === run.id} className="runs-grid">
            <span className="mono">#{shortId(run.id)}</span>
            <span className="run-task-cell">
              <strong title={run.task}>{run.task}</strong>
              <em title={run.workspace ?? undefined}>{run.workspace ?? "no workspace"}</em>
            </span>
            <span>
              <StatusBadge value={run.status} />
            </span>
            <span className="muted">{run.mode}</span>
            <span className="muted" title={formatDateTime(run.updated_at)}>
              {formatDistanceToNowStrict(new Date(run.updated_at))} ago
            </span>
            <span className="row-arrow">
              <ArrowRight size={16} aria-hidden />
            </span>
          </CompactRow>
        </Link>
      ))}
    </CompactList>
  );
}
