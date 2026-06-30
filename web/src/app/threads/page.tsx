"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ListFilter, MessageSquare, Plus, Search, Workflow } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import NewThreadDialog from "@/components/NewThreadDialog";
import { EmptyState, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import { archiveThread, listThreads } from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import type { ThreadStatus } from "@/types";

const STATUS_OPTIONS: Array<"all" | ThreadStatus> = ["active", "archived", "all"];

export default function ThreadsPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [status, setStatus] = useState<"all" | ThreadStatus>("active");
  const [search, setSearch] = useState("");
  const threadsQuery = useQuery({
    queryKey: ["threads", status, search],
    queryFn: () => listThreads(status, search),
    refetchInterval: 5000,
  });
  const archiveMutation = useMutation({
    mutationFn: archiveThread,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
    },
  });
  const threads = threadsQuery.data ?? [];
  const activeCount = useMemo(() => threads.filter((thread) => thread.status === "active").length, [threads]);
  const blockedCount = useMemo(
    () => threads.filter((thread) => thread.latest_run_status === "waiting_approval").length,
    [threads],
  );

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="workspace"
        title="Threads"
        description="Create and continue multi-agent work sessions."
        icon={MessageSquare}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Visible" value={threads.length} icon={Workflow} />
            <MetricTile label="Active" value={activeCount} />
            <MetricTile label="Blocked" value={blockedCount} />
          </div>
        }
      />
      <Panel
        title="Thread registry"
        action={
          <div className="filter-row">
            <ListFilter size={16} aria-hidden />
            <label className="filter-search">
              <Search size={15} aria-hidden />
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search threads" />
            </label>
            <select value={status} onChange={(event) => setStatus(event.target.value as "all" | ThreadStatus)}>
              {STATUS_OPTIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
            <button type="button" className="primary-button" onClick={() => setDialogOpen(true)}>
              <Plus size={16} aria-hidden />
              <span>New</span>
            </button>
          </div>
        }
      >
        {threadsQuery.error ? <div className="error-line">{threadsQuery.error.message}</div> : null}
        {threads.length ? (
          <div className="thread-list">
            {threads.map((thread) => (
              <article key={thread.id} className="thread-row">
                <Link href={`/threads/${thread.id}`} className="thread-row-main">
                  <span className="mono muted">{shortId(thread.id)}</span>
                  <span className="thread-row-copy">
                    <strong>{thread.title}</strong>
                    <em>{thread.last_message ?? "No messages yet"}</em>
                  </span>
                  <StatusBadge value={thread.latest_run_status ?? thread.status} />
                  <span className="muted">{formatDateTime(thread.updated_at)}</span>
                </Link>
                {thread.status === "active" ? (
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Archive ${thread.title}`}
                    disabled={archiveMutation.isPending}
                    onClick={() => archiveMutation.mutate(thread.id)}
                  >
                    <Archive size={16} aria-hidden />
                  </button>
                ) : (
                  <StatusBadge value="archived" />
                )}
              </article>
            ))}
          </div>
        ) : (
          <EmptyState title="No threads" />
        )}
      </Panel>
      <NewThreadDialog open={dialogOpen} onClose={() => setDialogOpen(false)} />
    </div>
  );
}
