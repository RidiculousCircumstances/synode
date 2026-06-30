"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Check,
  GitBranch,
  MessageSquare,
  Pencil,
  Play,
  RefreshCw,
  Send,
  X,
} from "lucide-react";
import Link from "next/link";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  archiveThread,
  createThreadRun,
  getThread,
  updateThread,
} from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import type { Run, RunMode, RunStatus, ThreadMessage } from "@/types";
import {
  CompactList,
  CompactRow,
  EmptyState,
  MetricTile,
  PageHeader,
  Panel,
  StatusBadge,
} from "@/components/ui/primitives";

const RUN_BUSY_STATUSES: RunStatus[] = ["created", "running", "waiting_approval"];

export default function ThreadDetailClient({ threadId }: { threadId: string }) {
  const queryClient = useQueryClient();
  const threadQuery = useQuery({
    queryKey: ["thread", threadId],
    queryFn: () => getThread(threadId),
    refetchInterval: 4000,
  });
  const detail = threadQuery.data ?? null;
  const thread = detail?.thread ?? null;
  const latestRun = detail?.runs[0] ?? null;
  const pending = latestRun ? RUN_BUSY_STATUSES.includes(latestRun.status) : false;

  const archiveMutation = useMutation({
    mutationFn: archiveThread,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
    },
  });

  if (threadQuery.isLoading && !detail) {
    return <EmptyState title="Loading thread" text={threadId} />;
  }

  if (threadQuery.error) {
    return <EmptyState title="Thread load failed" text={threadQuery.error.message} />;
  }

  if (!detail || !thread) {
    return <EmptyState title="Thread not found" text={threadId} />;
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow={`thread ${shortId(thread.id)}`}
        title={thread.title}
        description={thread.last_message ?? "No messages yet"}
        icon={MessageSquare}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Status" value={<StatusBadge value={thread.status} />} />
            <MetricTile label="Runs" value={detail.runs.length} icon={GitBranch} />
            <MetricTile label="Updated" value={formatDateTime(thread.updated_at)} />
          </div>
        }
      />
      <ThreadToolbar
        threadId={thread.id}
        title={thread.title}
        latestRun={latestRun}
        archived={thread.status === "archived"}
        archivePending={archiveMutation.isPending}
        onArchive={() => archiveMutation.mutate(thread.id)}
      />
      <div className="thread-workbench">
        <Panel title="Conversation" className="thread-conversation-panel">
          <ThreadMessages messages={detail.messages} />
          <FollowUpComposer
            threadId={thread.id}
            latestRun={latestRun}
            disabled={thread.status === "archived" || pending}
          />
        </Panel>
        <Panel title="Runs" className="thread-runs-panel">
          <ThreadRuns runs={detail.runs} />
        </Panel>
      </div>
    </div>
  );
}

function ThreadToolbar({
  threadId,
  title,
  latestRun,
  archived,
  archivePending,
  onArchive,
}: {
  threadId: string;
  title: string;
  latestRun: Run | null;
  archived: boolean;
  archivePending: boolean;
  onArchive: () => void;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const mutation = useMutation({
    mutationFn: (nextTitle: string) => updateThread(threadId, { title: nextTitle }),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
    },
  });

  useEffect(() => {
    setDraft(title);
  }, [title]);

  const save = () => {
    const next = draft.trim();
    if (!next || next === title) {
      setEditing(false);
      setDraft(title);
      return;
    }
    mutation.mutate(next);
  };

  return (
    <Panel
      title="Thread"
      action={
        <div className="thread-toolbar-actions">
          {latestRun ? (
            <Link className="secondary-button" href={`/runs/${latestRun.id}`}>
              <GitBranch size={15} aria-hidden />
              <span>Run {shortId(latestRun.id)}</span>
            </Link>
          ) : null}
          <button type="button" className="secondary-button" onClick={() => setEditing(true)} disabled={editing}>
            <Pencil size={15} aria-hidden />
            <span>Rename</span>
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={onArchive}
            disabled={archived || archivePending}
          >
            <Archive size={15} aria-hidden />
            <span>Archive</span>
          </button>
        </div>
      }
    >
      {editing ? (
        <div className="thread-title-edit">
          <input value={draft} onChange={(event) => setDraft(event.target.value)} autoFocus />
          <button
            type="button"
            className="icon-button approve"
            aria-label="Save title"
            disabled={mutation.isPending || !draft.trim()}
            onClick={save}
          >
            {mutation.isPending ? <RefreshCw size={16} className="spin" aria-hidden /> : <Check size={16} aria-hidden />}
          </button>
          <button
            type="button"
            className="icon-button reject"
            aria-label="Cancel title edit"
            onClick={() => {
              setEditing(false);
              setDraft(title);
            }}
          >
            <X size={16} aria-hidden />
          </button>
        </div>
      ) : (
        <div className="thread-toolbar-summary">
          <StatusBadge value={latestRun?.status ?? "idle"} />
          <span className="muted">{latestRun?.workspace ?? "No workspace configured"}</span>
          <span className="muted">{latestRun?.model_provider ?? "No provider yet"}</span>
        </div>
      )}
      {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
    </Panel>
  );
}

function ThreadMessages({ messages }: { messages: ThreadMessage[] }) {
  if (!messages.length) {
    return <EmptyState title="No messages" />;
  }

  return (
    <div className="thread-message-list">
      {messages.map((message) => (
        <article key={message.id} className={`thread-message ${message.author_type}`}>
          <div className="thread-message-meta">
            <StatusBadge value={message.author_type}>{message.author_name}</StatusBadge>
            <span>{message.message_type.replaceAll("_", " ")}</span>
            <em>{formatDateTime(message.created_at)}</em>
            {message.run_id ? (
              <Link href={`/runs/${message.run_id}`} className="mono">
                {shortId(message.run_id)}
              </Link>
            ) : null}
          </div>
          <div className="thread-message-body">{message.content}</div>
        </article>
      ))}
    </div>
  );
}

function FollowUpComposer({
  threadId,
  latestRun,
  disabled,
}: {
  threadId: string;
  latestRun: Run | null;
  disabled: boolean;
}) {
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [provider, setProvider] = useState(latestRun?.model_provider ?? "ollama");
  const [workspace, setWorkspace] = useState(latestRun?.workspace ?? "");
  const [mode, setMode] = useState<RunMode>(latestRun?.mode ?? "general");
  const mutation = useMutation({
    mutationFn: ({
      nextThreadId,
      payload,
    }: {
      nextThreadId: string;
      payload: Parameters<typeof createThreadRun>[1];
    }) => createThreadRun(nextThreadId, payload),
    onSuccess: () => {
      setMessage("");
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  useEffect(() => {
    setProvider(latestRun?.model_provider ?? "ollama");
    setWorkspace(latestRun?.workspace ?? "");
    setMode(latestRun?.mode ?? "general");
  }, [latestRun]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!message.trim() || disabled) {
      return;
    }
    mutation.mutate({
      nextThreadId: threadId,
      payload: {
        message: message.trim(),
        workspace: workspace.trim() || null,
        model_provider: provider.trim() || null,
        mode,
      },
    });
  };

  const disabledReason = useMemo(() => {
    if (!disabled) {
      return "";
    }
    if (latestRun && RUN_BUSY_STATUSES.includes(latestRun.status)) {
      return `Latest run is ${latestRun.status.replaceAll("_", " ")}.`;
    }
    return "Thread is archived.";
  }, [disabled, latestRun]);

  return (
    <form className="thread-followup-form" onSubmit={submit}>
      <label className="field">
        <span>Follow-up</span>
        <textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Continue this thread"
          rows={5}
          disabled={disabled}
        />
      </label>
      <div className="form-grid">
        <label className="field">
          <span>Mode</span>
          <select value={mode} onChange={(event) => setMode(event.target.value as RunMode)} disabled={disabled}>
            <option value="general">general</option>
            <option value="coding">coding</option>
          </select>
        </label>
        <label className="field">
          <span>Provider</span>
          <input value={provider} onChange={(event) => setProvider(event.target.value)} disabled={disabled} />
        </label>
      </div>
      <label className="field">
        <span>Workspace</span>
        <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} disabled={disabled} />
      </label>
      <div className="thread-followup-actions">
        {disabledReason ? <span className="muted">{disabledReason}</span> : null}
        <button className="primary-button" type="submit" disabled={mutation.isPending || disabled || !message.trim()}>
          {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Send size={16} aria-hidden />}
          <span>Send</span>
        </button>
      </div>
      {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
    </form>
  );
}

function ThreadRuns({ runs }: { runs: Run[] }) {
  if (!runs.length) {
    return <EmptyState title="No runs" />;
  }

  return (
    <CompactList className="thread-runs-list">
      {runs.map((run) => (
        <Link key={run.id} href={`/runs/${run.id}`} className="row-link">
          <CompactRow className="thread-run-row">
            <span className="mono">{shortId(run.id)}</span>
            <StatusBadge value={run.status} />
            <strong>{run.task}</strong>
            <em>{formatDateTime(run.updated_at)}</em>
            <Play size={15} aria-hidden />
          </CompactRow>
        </Link>
      ))}
    </CompactList>
  );
}
