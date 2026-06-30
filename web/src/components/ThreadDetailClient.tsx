"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Check,
  GitBranch,
  History,
  Pencil,
  Play,
  RefreshCw,
  Send,
  SlidersHorizontal,
  X,
} from "lucide-react";
import Link from "next/link";
import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import {
  archiveThread,
  createThreadRun,
  decideApproval,
  getThread,
  listAgentGraphs,
  listModelProfiles,
  resumeRun,
  stopRun,
  updateThread,
} from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useRunEvents } from "@/hooks/useRunEvents";
import type { Run, RunEvent, RunMode, RunStatus, ThreadMessage } from "@/types";
import {
  CompactList,
  CompactRow,
  EmptyState,
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
  const liveEvents = useRunEvents(pending && latestRun ? latestRun.id : null);
  const processingStatus = useMemo(
    () => buildProcessingStatus(latestRun, liveEvents),
    [latestRun, liveEvents],
  );
  const streamingOutput = useMemo(() => buildStreamingOutput(liveEvents), [liveEvents]);
  const [runsOpen, setRunsOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const didInitialScroll = useRef(false);
  const wasNearBottom = useRef(true);
  const lastMessageId = detail?.messages.at(-1)?.id ?? 0;
  const lastEventId = liveEvents.at(-1)?.id ?? 0;

  const archiveMutation = useMutation({
    mutationFn: archiveThread,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
    },
  });
  const stopMutation = useMutation({
    mutationFn: (runId: string) => stopRun(runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  useEffect(() => {
    didInitialScroll.current = false;
    wasNearBottom.current = true;
  }, [threadId]);

  useEffect(() => {
    if (!detail) {
      return;
    }
    const shouldScroll = !didInitialScroll.current || wasNearBottom.current;
    if (!shouldScroll) {
      return;
    }
    window.requestAnimationFrame(() => {
      scrollToBottom(scrollRef.current, didInitialScroll.current ? "smooth" : "auto");
      didInitialScroll.current = true;
      wasNearBottom.current = true;
    });
  }, [detail, lastMessageId, lastEventId]);

  useEffect(() => {
    if (!lastEventId) {
      return;
    }
    void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
    void queryClient.invalidateQueries({ queryKey: ["threads"] });
  }, [lastEventId, queryClient, threadId]);

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
    <div className="thread-chat-shell">
      <ThreadTopBar
        threadId={thread.id}
        title={thread.title}
        threadStatus={thread.status}
        latestRun={latestRun}
        runsCount={detail.runs.length}
        archived={thread.status === "archived"}
        archivePending={archiveMutation.isPending}
        stopPending={stopMutation.isPending}
        onArchive={() => archiveMutation.mutate(thread.id)}
        onStopRun={latestRun && pending ? () => stopMutation.mutate(latestRun.id) : undefined}
        onOpenRuns={() => setRunsOpen(true)}
      />
      {stopMutation.error ? <div className="error-line thread-inline-error">{stopMutation.error.message}</div> : null}
      <div
        ref={scrollRef}
        className="thread-message-scroll"
        onScroll={(event) => {
          wasNearBottom.current = isNearBottom(event.currentTarget);
        }}
      >
        <ThreadMessages
          threadId={thread.id}
          messages={detail.messages}
          processingStatus={processingStatus}
          streamingOutput={streamingOutput}
        />
      </div>
      <div className="thread-composer-dock">
        <FollowUpComposer
          threadId={thread.id}
          latestRun={latestRun}
          disabled={thread.status === "archived" || pending}
        />
      </div>
      <RunHistoryDrawer open={runsOpen} runs={detail.runs} onClose={() => setRunsOpen(false)} />
    </div>
  );
}

function ThreadTopBar({
  threadId,
  title,
  threadStatus,
  latestRun,
  runsCount,
  archived,
  archivePending,
  stopPending,
  onArchive,
  onStopRun,
  onOpenRuns,
}: {
  threadId: string;
  title: string;
  threadStatus: string;
  latestRun: Run | null;
  runsCount: number;
  archived: boolean;
  archivePending: boolean;
  stopPending: boolean;
  onArchive: () => void;
  onStopRun?: () => void;
  onOpenRuns: () => void;
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
    <header className="thread-topbar">
      <div className="thread-topbar-main">
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
          <div className="thread-title-line">
            <h1 title={title}>{title}</h1>
            <StatusBadge value={threadStatus} />
            <StatusBadge value={latestRun?.status ?? "idle"} />
            {latestRun?.workspace ? <span className="thread-topbar-meta">{latestRun.workspace}</span> : null}
            {latestRun?.model_provider ? <span className="thread-topbar-meta">{latestRun.model_provider}</span> : null}
            {latestRun ? <span className="thread-topbar-meta mono">{shortId(latestRun.id)}</span> : null}
            {latestRun ? <span className="thread-topbar-meta">{formatDateTime(latestRun.updated_at)}</span> : null}
          </div>
        )}
        {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
      </div>
      {!editing ? (
        <div className="thread-toolbar-actions">
          {onStopRun ? (
            <button
              type="button"
              className="icon-button reject thread-topbar-action"
              onClick={onStopRun}
              disabled={stopPending}
              aria-label="Stop latest run"
              title="Stop latest run"
            >
              {stopPending ? <RefreshCw size={15} className="spin" aria-hidden /> : <X size={15} aria-hidden />}
            </button>
          ) : null}
          {latestRun ? (
            <Link className="icon-button thread-topbar-action" href={`/runs/${latestRun.id}`} aria-label="Open latest run" title="Open latest run">
              <GitBranch size={15} aria-hidden />
            </Link>
          ) : null}
          <button type="button" className="icon-button thread-topbar-action" onClick={onOpenRuns} aria-label={`Runs ${runsCount}`} title={`Runs ${runsCount}`}>
            <History size={15} aria-hidden />
          </button>
          <button type="button" className="icon-button thread-topbar-action" onClick={() => setEditing(true)} disabled={editing} aria-label="Rename thread" title="Rename thread">
            <Pencil size={15} aria-hidden />
          </button>
          <button
            type="button"
            className="icon-button thread-topbar-action"
            onClick={onArchive}
            disabled={archived || archivePending}
            aria-label="Archive thread"
            title="Archive thread"
          >
            <Archive size={15} aria-hidden />
          </button>
        </div>
      ) : null}
    </header>
  );
}

function ThreadMessages({
  threadId,
  messages,
  processingStatus,
  streamingOutput,
}: {
  threadId: string;
  messages: ThreadMessage[];
  processingStatus: ProcessingStatus | null;
  streamingOutput: StreamingOutput | null;
}) {
  const approvalDecisions = useMemo(() => buildApprovalDecisionMap(messages), [messages]);

  if (!messages.length && !processingStatus && !streamingOutput) {
    return <EmptyState title="No messages" />;
  }

  return (
    <div className="thread-message-list">
      {messages.map((message) => (
        <ThreadMessageItem
          key={message.id}
          threadId={threadId}
          message={message}
          approvalDecision={approvalDecisions.get(metadataString(message.metadata, "approval_id")) ?? null}
        />
      ))}
      {streamingOutput ? <ThreadStreamingMessage output={streamingOutput} /> : null}
      {processingStatus ? <ThreadProcessingEvent status={processingStatus} /> : null}
    </div>
  );
}

function ThreadMessageItem({
  threadId,
  message,
  approvalDecision,
}: {
  threadId: string;
  message: ThreadMessage;
  approvalDecision: ApprovalDecision | null;
}) {
  if (isServiceEvent(message)) {
    return <ThreadServiceEvent threadId={threadId} message={message} approvalDecision={approvalDecision} />;
  }

  return (
    <article className={`thread-message ${message.author_type}`}>
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
      <div className="thread-message-body">
        <ThreadMessageContent message={message} />
      </div>
    </article>
  );
}

function ThreadServiceEvent({
  threadId,
  message,
  approvalDecision,
}: {
  threadId: string;
  message: ThreadMessage;
  approvalDecision: ApprovalDecision | null;
}) {
  if (message.message_type === "approval_request") {
    return <ThreadApprovalEvent threadId={threadId} message={message} approvalDecision={approvalDecision} />;
  }

  const metadata = Object.keys(message.metadata).length ? JSON.stringify(message.metadata, null, 2) : "";
  return (
    <div className="thread-service-event neutral">
      <div className="thread-service-copy">
        <span className="thread-service-kind">{message.message_type.replaceAll("_", " ")}</span>
        <span className="thread-service-text">{message.content}</span>
        <em>{formatDateTime(message.created_at)}</em>
        {message.run_id ? (
          <Link href={`/runs/${message.run_id}`} className="mono">
            {shortId(message.run_id)}
          </Link>
        ) : null}
        {metadata ? (
          <details>
            <summary>details</summary>
            <pre>{metadata}</pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function ThreadProcessingEvent({ status }: { status: ProcessingStatus }) {
  return (
    <div className="thread-service-event live" aria-live="polite">
      <div className="thread-service-copy">
        <RefreshCw size={13} className={status.spinning ? "spin" : undefined} aria-hidden />
        <span className="thread-service-kind">{status.kind}</span>
        <span className="thread-service-text">{status.text}</span>
        {status.runId ? (
          <Link href={`/runs/${status.runId}`} className="mono">
            {shortId(status.runId)}
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function ThreadStreamingMessage({ output }: { output: StreamingOutput }) {
  return (
    <article className="thread-message agent thread-streaming-message" aria-live="polite">
      <div className="thread-message-meta">
        <StatusBadge value="agent">{output.role}</StatusBadge>
        <span>{output.completed ? "streamed output" : "streaming output"}</span>
      </div>
      <div className="thread-message-body">
        {output.content.trim() ? (
          <MarkdownContent content={output.content} />
        ) : (
          <span className="muted">Waiting for model output...</span>
        )}
      </div>
    </article>
  );
}

function ThreadApprovalEvent({
  threadId,
  message,
  approvalDecision,
}: {
  threadId: string;
  message: ThreadMessage;
  approvalDecision: ApprovalDecision | null;
}) {
  const metadata = Object.keys(message.metadata).length ? JSON.stringify(message.metadata, null, 2) : "";
  const toolName = metadataString(message.metadata, "tool_name");
  const action = metadataString(message.metadata, "action");
  return (
    <article className="thread-approval-event">
      <div className="thread-approval-event-header">
        <span className="thread-service-kind">Approval</span>
        <span>{formatDateTime(message.created_at)}</span>
        {message.run_id ? (
          <Link href={`/runs/${message.run_id}`} className="mono">
            {shortId(message.run_id)}
          </Link>
        ) : null}
      </div>
      <div className="thread-approval-event-body">
        <MarkdownContent content={approvalReasonText(message.content, toolName)} />
      </div>
      <div className="thread-approval-event-footer">
        <span className="thread-approval-target">
          {toolName ? <strong>{toolName}</strong> : null}
          {action ? <em>{action}</em> : null}
        </span>
        <ApprovalInlineActions threadId={threadId} message={message} decision={approvalDecision} />
      </div>
      {metadata ? (
        <details className="thread-approval-details">
          <summary>details</summary>
          <pre>{metadata}</pre>
        </details>
      ) : null}
    </article>
  );
}

function ApprovalInlineActions({
  threadId,
  message,
  decision,
}: {
  threadId: string;
  message: ThreadMessage;
  decision: ApprovalDecision | null;
}) {
  const queryClient = useQueryClient();
  const approvalId = metadataString(message.metadata, "approval_id");
  const toolName = metadataString(message.metadata, "tool_name");
  const action = metadataString(message.metadata, "action");
  const mutation = useMutation({
    mutationFn: async ({ nextDecision }: { nextDecision: "approve" | "reject" }) => {
      await decideApproval(approvalId, nextDecision, `${nextDecision} from Synode thread chat`);
      if (nextDecision === "approve") {
        if (!message.run_id) {
          throw new Error("Approved approval has no run id to resume.");
        }
        await resumeRun(message.run_id);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["thread", threadId] });
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      if (message.run_id) {
        void queryClient.invalidateQueries({ queryKey: ["run", message.run_id] });
        void queryClient.invalidateQueries({ queryKey: ["approvals", message.run_id] });
        void queryClient.invalidateQueries({ queryKey: ["run-metrics", message.run_id] });
      }
    },
  });

  if (!approvalId) {
    return null;
  }

  if (decision) {
    return (
      <span className="thread-approval-decision">
        <StatusBadge value={decision.status} />
        {decision.reason ? <span>{decision.reason}</span> : null}
      </span>
    );
  }

  return (
    <span className="thread-approval-inline">
      <span className="thread-approval-target">
        {toolName ? <strong>{toolName}</strong> : null}
        {action ? <em>{action}</em> : null}
      </span>
      <span className="approval-actions">
        <button
          className="icon-button approve"
          title="Approve"
          aria-label="Approve"
          type="button"
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ nextDecision: "approve" })}
        >
          {mutation.isPending ? <RefreshCw size={15} className="spin" aria-hidden /> : <Check size={15} aria-hidden />}
        </button>
        <button
          className="icon-button reject"
          title="Reject"
          aria-label="Reject"
          type="button"
          disabled={mutation.isPending}
          onClick={() => mutation.mutate({ nextDecision: "reject" })}
        >
          <X size={15} aria-hidden />
        </button>
      </span>
      {mutation.error ? <span className="error-line">{mutation.error.message}</span> : null}
    </span>
  );
}

function ThreadMessageContent({ message }: { message: ThreadMessage }) {
  if (message.content.startsWith("Synode run summary:")) {
    return <RunSummaryContent content={message.content} />;
  }
  return <GenericMessageContent content={message.content} />;
}

function GenericMessageContent({ content }: { content: string }) {
  const parsed = splitTechnicalLines(content);
  return (
    <div className="thread-message-content">
      {parsed.text ? <MarkdownContent content={parsed.text} /> : null}
      {parsed.technical.length ? (
        <TechnicalDetails title="Technical details" lines={parsed.technical} />
      ) : null}
    </div>
  );
}

function RunSummaryContent({ content }: { content: string }) {
  const summary = parseRunSummary(content);
  return (
    <div className="run-summary-content">
      <div className="run-summary-topline">
        <strong>Run summary</strong>
        {summary.mode ? <span className="chat-tech-chip">mode {summary.mode}</span> : null}
      </div>
      {summary.plan.length ? (
        <details className="technical-details compact">
          <summary>Plan</summary>
          <div className="summary-plan-list">
            {summary.plan.map((item, index) => (
              <div key={`${item.role}-${index}`} className="summary-plan-row">
                <StatusBadge value={item.role} />
                <span>{item.task}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
      {summary.sections.map((section, index) => (
        <SummarySectionView key={`${section.title}-${index}`} section={section} />
      ))}
      {summary.blockers.length ? <IssueList title="Blockers" tone="warning" items={summary.blockers} /> : null}
      {summary.advisory.length ? <IssueList title="Advisory risks" tone="neutral" items={summary.advisory} /> : null}
    </div>
  );
}

function SummarySectionView({ section }: { section: SummarySection }) {
  const split = splitTechnicalLines(section.body);
  if (section.technical) {
    return <TechnicalDetails title={section.title} lines={[section.body]} />;
  }
  return (
    <section className="chat-role-section">
      <div className="chat-section-title">
        <StatusBadge value={section.title} />
      </div>
      {split.text ? <MarkdownContent content={split.text} /> : null}
      {split.technical.length ? <TechnicalDetails title="Tool and raw output" lines={split.technical} /> : null}
    </section>
  );
}

function MarkdownContent({ content }: { content: string }) {
  const blocks = parseMarkdownBlocks(content);
  return (
    <div className="chat-markdown">
      {blocks.map((block, index) => {
        if (block.type === "code") {
          return (
            <figure key={index} className="chat-code-block">
              {block.language ? <figcaption>{block.language}</figcaption> : null}
              <pre>
                <code>{block.code}</code>
              </pre>
            </figure>
          );
        }
        if (block.type === "heading") {
          const Heading = `h${block.depth}` as "h2" | "h3" | "h4";
          return <Heading key={index}>{renderInlineMarkdown(block.text)}</Heading>;
        }
        if (block.type === "blockquote") {
          return <blockquote key={index}>{renderInlineMarkdown(block.text)}</blockquote>;
        }
        if (block.type === "list") {
          const List = block.ordered ? "ol" : "ul";
          return (
            <List key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInlineMarkdown(item)}</li>
              ))}
            </List>
          );
        }
        return <p key={index}>{renderInlineMarkdown(block.text)}</p>;
      })}
    </div>
  );
}

function TechnicalDetails({ title, lines }: { title: string; lines: string[] }) {
  const value = lines.join("\n").trim();
  if (!value) {
    return null;
  }
  return (
    <details className="technical-details">
      <summary>{title}</summary>
      <pre>{value}</pre>
    </details>
  );
}

function IssueList({
  title,
  tone,
  items,
}: {
  title: string;
  tone: "neutral" | "warning";
  items: string[];
}) {
  return (
    <section className={`issue-list ${tone}`}>
      <strong>{title}</strong>
      <ul>
        {items.map((item, index) => (
          <li key={`${title}-${index}`}>{item}</li>
        ))}
      </ul>
    </section>
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
  const [profileId, setProfileId] = useState(latestRun?.default_model_profile_id ?? "");
  const [graphId, setGraphId] = useState(latestRun?.agent_graph_id ?? "");
  const [workspace, setWorkspace] = useState(latestRun?.workspace ?? "");
  const [mode, setMode] = useState<RunMode>(latestRun?.mode ?? "general");
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles });
  const graphsQuery = useQuery({ queryKey: ["agent-graphs"], queryFn: listAgentGraphs });
  const profiles = profilesQuery.data ?? [];
  const graphs = graphsQuery.data ?? [];
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
    setProfileId(latestRun?.default_model_profile_id ?? "");
    setGraphId(latestRun?.agent_graph_id ?? "");
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
        model_provider: null,
        default_model_profile_id: profileId || null,
        agent_graph_id: graphId || null,
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
      <div className="thread-composer-input-row">
        <details className="thread-composer-settings">
          <summary aria-label="Run settings" title="Run settings">
            <SlidersHorizontal size={16} aria-hidden />
          </summary>
          <div className="thread-composer-settings-panel">
            <div className="form-grid">
              <label className="field">
                <span>Mode</span>
                <select value={mode} onChange={(event) => setMode(event.target.value as RunMode)} disabled={disabled}>
                  <option value="general">general</option>
                  <option value="coding">coding</option>
                </select>
              </label>
              <label className="field">
                <span>Model profile</span>
                <select value={profileId} onChange={(event) => setProfileId(event.target.value)} disabled={disabled}>
                  <option value="">thread/default</option>
                  {profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="field">
              <span>Agent graph</span>
              <select value={graphId} onChange={(event) => setGraphId(event.target.value)} disabled={disabled}>
                <option value="">thread/default</option>
                {graphs.map((graph) => (
                  <option key={graph.id} value={graph.id}>
                    {graph.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Workspace</span>
              <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} disabled={disabled} />
            </label>
            {profilesQuery.error ? <div className="error-line">{profilesQuery.error.message}</div> : null}
            {graphsQuery.error ? <div className="error-line">{graphsQuery.error.message}</div> : null}
          </div>
        </details>
        <input
          className="thread-composer-input"
          aria-label="Message"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder={disabledReason || "Message Synode"}
          disabled={disabled}
        />
        <button
          className="icon-button thread-send-button"
          type="submit"
          aria-label="Send"
          title="Send"
          disabled={mutation.isPending || disabled || !message.trim()}
        >
          {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Send size={16} aria-hidden />}
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

function RunHistoryDrawer({
  open,
  runs,
  onClose,
}: {
  open: boolean;
  runs: Run[];
  onClose: () => void;
}) {
  useBodyScrollLock(open);

  if (!open) {
    return null;
  }

  return (
    <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="Run history">
      <button type="button" className="drawer-backdrop" aria-label="Close run history" onClick={onClose} />
      <aside className="drawer-panel">
        <header className="drawer-header">
          <div>
            <h2>Runs</h2>
            <span>{runs.length} execution attempts</span>
          </div>
          <button type="button" className="icon-button" aria-label="Close run history" onClick={onClose}>
            <X size={16} aria-hidden />
          </button>
        </header>
        <ThreadRuns runs={runs} />
      </aside>
    </div>
  );
}

type SummarySection = {
  title: string;
  body: string;
  technical: boolean;
};

type ParsedSummary = {
  mode: string | null;
  plan: Array<{ role: string; task: string }>;
  sections: SummarySection[];
  blockers: string[];
  advisory: string[];
};

type ApprovalDecision = {
  status: string;
  reason: string;
};

type ProcessingStatus = {
  kind: string;
  text: string;
  runId: string;
  spinning: boolean;
};

type StreamingOutput = {
  streamId: string;
  role: string;
  content: string;
  completed: boolean;
};

type MarkdownBlock =
  | { type: "paragraph"; text: string }
  | { type: "heading"; depth: 2 | 3 | 4; text: string }
  | { type: "blockquote"; text: string }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "code"; language: string; code: string };

const TECHNICAL_SECTION_NAMES = new Set([
  "coding_inspection",
  "patch_proposal",
  "patch_results",
  "verification",
]);

const STREAM_EVENT_TYPES = new Set([
  "model_stream_started",
  "model_token_delta",
  "model_stream_completed",
]);

function buildProcessingStatus(run: Run | null, events: RunEvent[]): ProcessingStatus | null {
  if (!run || !RUN_BUSY_STATUSES.includes(run.status)) {
    return null;
  }
  if (run.status === "waiting_approval") {
    return {
      kind: "approval",
      text: "Waiting for approval",
      runId: run.id,
      spinning: false,
    };
  }
  const latest = [...events].reverse().find((event) => event.run_id === run.id);
  if (!latest) {
    return {
      kind: "runtime",
      text: run.status === "created" ? "Queued" : "Starting run",
      runId: run.id,
      spinning: true,
    };
  }
  return {
    kind: latest.event_type.replaceAll("_", " "),
    text: describeRunEvent(latest),
    runId: run.id,
    spinning: isSpinningEvent(latest.event_type),
  };
}

function describeRunEvent(event: RunEvent): string {
  const payload = event.payload;
  if (event.event_type === "model_stream_started") {
    const role = event.role || metadataString(payload, "role") || "model";
    return `Streaming output from ${role}`;
  }
  if (event.event_type === "model_token_delta") {
    const role = event.role || metadataString(payload, "role") || "model";
    return `Receiving output from ${role}`;
  }
  if (event.event_type === "model_stream_completed") {
    const role = event.role || metadataString(payload, "role") || "model";
    return `Completed output from ${role}`;
  }
  if (event.event_type === "node_started") {
    const node = metadataString(payload, "node") || "node";
    const role = event.role ? ` (${event.role})` : "";
    return `Running ${node.replaceAll("_", " ")}${role}`;
  }
  if (event.event_type === "tool_called") {
    const tool = metadataString(payload, "tool_name") || "tool";
    const status = metadataString(payload, "status");
    return status ? `${tool} ${status}` : `Calling ${tool}`;
  }
  if (event.event_type === "model_invoked") {
    const role = event.role || metadataString(payload, "role") || "model";
    return `Model response from ${role}`;
  }
  if (event.event_type === "approval_required") {
    const tool = metadataString(payload, "tool_name");
    return tool ? `Waiting for approval: ${tool}` : "Waiting for approval";
  }
  if (event.event_type === "approval_decided") {
    return "Approval recorded. Continuing run";
  }
  if (event.event_type === "artifact_created") {
    const kind = metadataString(payload, "kind");
    return kind ? `Saved ${kind.replaceAll("_", " ")}` : "Saved artifact";
  }
  if (event.event_type === "verification_completed") {
    return "Verification completed";
  }
  if (event.event_type === "role_selected") {
    const role = event.role || metadataString(payload, "role");
    return role ? `Selected ${role}` : "Selected roles";
  }
  if (event.event_type === "run_started") {
    return "Run started";
  }
  if (event.event_type === "intake_completed") {
    return "Task intake completed";
  }
  return event.event_type.replaceAll("_", " ");
}

function isSpinningEvent(eventType: string): boolean {
  return ![
    "approval_required",
    "verification_completed",
    "artifact_created",
    "model_stream_completed",
  ].includes(eventType);
}

function buildStreamingOutput(events: RunEvent[]): StreamingOutput | null {
  const streamEvents = events.filter((event) => STREAM_EVENT_TYPES.has(event.event_type));
  const latestStart = [...streamEvents]
    .reverse()
    .find((event) => event.event_type === "model_stream_started");
  if (!latestStart) {
    return null;
  }
  const streamId = metadataString(latestStart.payload, "stream_id");
  if (!streamId) {
    return null;
  }
  const role = latestStart.role || metadataString(latestStart.payload, "role") || "agent";
  const content = streamEvents
    .filter(
      (event) =>
        event.event_type === "model_token_delta" &&
        metadataString(event.payload, "stream_id") === streamId,
    )
    .map((event) => metadataString(event.payload, "delta"))
    .join("");
  const completed = streamEvents.some(
    (event) =>
      event.event_type === "model_stream_completed" &&
      metadataString(event.payload, "stream_id") === streamId,
  );
  return { streamId, role, content, completed };
}

function parseRunSummary(content: string): ParsedSummary {
  const lines = content.split("\n");
  let mode: string | null = null;
  const plan: Array<{ role: string; task: string }> = [];
  const sections: SummarySection[] = [];
  const blockers: string[] = [];
  const advisory: string[] = [];
  let current: SummarySection | null = null;
  let issueTarget: string[] | null = null;

  const flush = () => {
    if (current && current.body.trim()) {
      sections.push({ ...current, body: current.body.trim() });
    }
    current = null;
  };

  for (const rawLine of lines.slice(1)) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      if (current) {
        current.body += "\n";
      }
      continue;
    }
    const sectionMatch = trimmed.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      flush();
      issueTarget = null;
      const title = sectionMatch[1];
      current = {
        title,
        body: "",
        technical: TECHNICAL_SECTION_NAMES.has(title),
      };
      continue;
    }
    if (trimmed === "Blockers:") {
      flush();
      issueTarget = blockers;
      continue;
    }
    if (trimmed === "Advisory risks:") {
      flush();
      issueTarget = advisory;
      continue;
    }
    if (issueTarget) {
      issueTarget.push(trimmed.replace(/^- /, ""));
      continue;
    }
    if (trimmed.startsWith("Mode:")) {
      mode = trimmed.replace(/^Mode:\s*/, "");
      continue;
    }
    const planMatch = trimmed.match(/^- ([^:]+):\s*(.+)$/);
    if (planMatch && !current) {
      plan.push({ role: planMatch[1], task: planMatch[2] });
      continue;
    }
    if (current) {
      current.body += `${line}\n`;
    }
  }
  flush();
  return { mode, plan, sections, blockers, advisory };
}

function splitTechnicalLines(content: string): { text: string; technical: string[] } {
  const textLines: string[] = [];
  const technical: string[] = [];
  for (const line of content.split("\n")) {
    const trimmed = line.trim();
    if (isTechnicalLine(trimmed)) {
      technical.push(line);
    } else {
      textLines.push(line);
    }
  }
  return {
    text: textLines.join("\n").trim(),
    technical: compactTechnicalLines(technical),
  };
}

function isTechnicalLine(value: string): boolean {
  if (!value) {
    return false;
  }
  return (
    value.startsWith("Mode:") ||
    value.startsWith("- native.") ||
    value.startsWith("- mcp.") ||
    value.startsWith("{") ||
    value.startsWith("[{") ||
    /^[-\w.]+:\s*(ok|failed|denied)\b/.test(value)
  );
}

function compactTechnicalLines(lines: string[]): string[] {
  return lines.map((line) => {
    if (line.length <= 1400) {
      return line;
    }
    return `${line.slice(0, 1400)}\n... truncated in chat view; open the run for full artifacts.`;
  });
}

function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: { language: string; lines: string[] } | null = null;

  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    if (text) {
      blocks.push({ type: "paragraph", text });
    }
    paragraph.length = 0;
  };

  const flushList = () => {
    if (list?.items.length) {
      blocks.push({ type: "list", ordered: list.ordered, items: list.items });
    }
    list = null;
  };

  const flushText = () => {
    flushParagraph();
    flushList();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    if (code) {
      if (trimmed === "```") {
        blocks.push({ type: "code", language: code.language, code: code.lines.join("\n").trimEnd() });
        code = null;
      } else {
        code.lines.push(line);
      }
      continue;
    }

    const codeStart = trimmed.match(/^```([\w.+-]*)\s*$/);
    if (codeStart) {
      flushText();
      code = { language: codeStart[1] ?? "", lines: [] };
      continue;
    }

    if (!trimmed) {
      flushText();
      continue;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushText();
      const depth = Math.min(4, Math.max(2, heading[1].length + 1)) as 2 | 3 | 4;
      blocks.push({ type: "heading", depth, text: heading[2].trim() });
      continue;
    }

    if (trimmed.startsWith(">")) {
      flushText();
      const quoteLines = [trimmed.replace(/^>\s?/, "")];
      while (index + 1 < lines.length && lines[index + 1].trim().startsWith(">")) {
        index += 1;
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ""));
      }
      blocks.push({ type: "blockquote", text: quoteLines.join(" ").trim() });
      continue;
    }

    const unordered = trimmed.match(/^[-*+]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const isOrdered = Boolean(ordered);
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push((ordered?.[1] ?? unordered?.[1] ?? "").trim());
      continue;
    }

    flushList();
    paragraph.push(trimmed);
  }

  if (code) {
    blocks.push({ type: "code", language: code.language, code: code.lines.join("\n").trimEnd() });
  }
  flushText();
  return blocks;
}

function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*)/g;
  let lastIndex = 0;
  let key = 0;

  for (const match of text.matchAll(pattern)) {
    const matchIndex = match.index ?? 0;
    if (matchIndex > lastIndex) {
      nodes.push(text.slice(lastIndex, matchIndex));
    }
    if (match[2]) {
      nodes.push(<code key={`code-${key}`}>{match[2]}</code>);
    } else if (match[3] && match[4]) {
      const href = safeMarkdownHref(match[4]);
      nodes.push(
        href ? (
          <a key={`link-${key}`} href={href} target={href.startsWith("http") ? "_blank" : undefined} rel="noreferrer">
            {match[3]}
          </a>
        ) : (
          match[3]
        ),
      );
    } else if (match[5]) {
      nodes.push(<strong key={`strong-${key}`}>{match[5]}</strong>);
    }
    key += 1;
    lastIndex = matchIndex + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

function safeMarkdownHref(value: string): string | null {
  if (value.startsWith("/") || value.startsWith("#") || value.startsWith("http://") || value.startsWith("https://")) {
    return value;
  }
  return null;
}

function approvalReasonText(content: string, toolName: string): string {
  const exactPrefix = toolName ? `Approval required for ${toolName}:` : "";
  const stripped =
    exactPrefix && content.startsWith(exactPrefix)
      ? content.slice(exactPrefix.length).trim()
      : content.replace(/^Approval required for [^:]+:\s*/, "").trim();
  return stripped || content;
}

function buildApprovalDecisionMap(messages: ThreadMessage[]): Map<string, ApprovalDecision> {
  const decisions = new Map<string, ApprovalDecision>();
  for (const message of messages) {
    if (message.message_type !== "approval_decision") {
      continue;
    }
    const approvalId = metadataString(message.metadata, "approval_id");
    const status = metadataString(message.metadata, "status");
    if (approvalId && status) {
      decisions.set(approvalId, { status, reason: message.content });
    }
  }
  return decisions;
}

function metadataString(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key];
  return typeof value === "string" ? value : "";
}

function isServiceEvent(message: ThreadMessage): boolean {
  if (message.content.startsWith("Synode run summary:")) {
    return false;
  }
  return (
    message.message_type === "run_summary" ||
    message.message_type === "approval_request" ||
    message.message_type === "approval_decision"
  );
}

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 160;
}

function scrollToBottom(element: HTMLElement | null, behavior: ScrollBehavior) {
  if (!element) {
    return;
  }
  element.scrollTo({ top: element.scrollHeight, behavior });
}
