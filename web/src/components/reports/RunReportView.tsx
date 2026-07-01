"use client";

import { AlertTriangle, CheckCircle2, FileDiff, ListChecks, TerminalSquare, Wrench, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { StatusBadge } from "@/components/ui/primitives";
import type { RunReport } from "@/types";

type Density = "chat" | "detail";

export function RunReportView({ report, density = "detail" }: { report: RunReport; density?: Density }) {
  const compact = density === "chat";
  return (
    <section className={compact ? "run-report run-report-chat" : "run-report"}>
      <div className="run-report-head">
        <div>
          <strong>{report.headline}</strong>
          {report.summary ? <p>{report.summary}</p> : null}
        </div>
        <StatusBadge value={report.status} />
      </div>
      {report.plan.length ? <PlanReportView report={report} compact={compact} /> : null}
      <div className="run-report-grid">
        <PatchResultsView report={report} />
        <VerificationView report={report} />
      </div>
      {report.role_outputs.length ? <RoleOutputsView report={report} compact={compact} /> : null}
      {report.tool_activity.length ? <ToolActivityView report={report} compact={compact} /> : null}
      {report.blockers.length ? <IssueStrip title="Blockers" items={report.blockers} tone="danger" /> : null}
      {!compact && report.advisory.length ? <IssueStrip title="Advisory" items={report.advisory} tone="warning" /> : null}
      {!compact && Object.keys(report.diagnostics).length ? (
        <details className="technical-details compact">
          <summary>Diagnostics</summary>
          <pre>{JSON.stringify(report.diagnostics, null, 2)}</pre>
        </details>
      ) : null}
    </section>
  );
}

export function reportFromMessageMetadata(metadata: Record<string, unknown>): RunReport | null {
  const value = metadata.run_report;
  if (!value || typeof value !== "object") {
    return null;
  }
  const candidate = value as Partial<RunReport>;
  if (typeof candidate.headline !== "string" || typeof candidate.status !== "string") {
    return null;
  }
  return {
    version: numberOr(candidate.version, 1),
    run_id: stringOr(candidate.run_id),
    thread_id: stringOr(candidate.thread_id),
    mode: stringOr(candidate.mode),
    status: candidate.status,
    headline: candidate.headline,
    summary: stringOr(candidate.summary),
    plan: Array.isArray(candidate.plan) ? candidate.plan : [],
    role_outputs: Array.isArray(candidate.role_outputs) ? candidate.role_outputs : [],
    patch_results:
      candidate.patch_results && typeof candidate.patch_results === "object"
        ? candidate.patch_results
        : { status: "not_applicable", files: [], raw_count: 0 },
    verification:
      candidate.verification && typeof candidate.verification === "object"
        ? candidate.verification
        : { status: "not_run", commands: [], reason: null },
    tool_activity: Array.isArray(candidate.tool_activity) ? candidate.tool_activity : [],
    blockers: Array.isArray(candidate.blockers) ? candidate.blockers : [],
    advisory: Array.isArray(candidate.advisory) ? candidate.advisory : [],
    diagnostics:
      candidate.diagnostics && typeof candidate.diagnostics === "object"
        ? (candidate.diagnostics as Record<string, unknown>)
        : {},
    raw_refs:
      candidate.raw_refs && typeof candidate.raw_refs === "object"
        ? (candidate.raw_refs as Record<string, string>)
        : {},
    artifact_id: candidate.artifact_id,
    created_at: candidate.created_at,
  };
}

function PlanReportView({ report, compact }: { report: RunReport; compact: boolean }) {
  const items = compact ? report.plan.slice(0, 4) : report.plan;
  return (
    <section className="run-report-section">
      <h3>
        <ListChecks size={15} aria-hidden />
        Plan
      </h3>
      <ol className="run-report-plan">
        {items.map((step, index) => (
          <li key={`${step.role}-${index}`}>
            <StatusBadge value={step.role} />
            <span>{step.task}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function PatchResultsView({ report }: { report: RunReport }) {
  const patch = report.patch_results;
  if (patch.status === "not_applicable") {
    return (
      <ReportTile icon={FileDiff} title="Patch" status="not run">
        <span>No patch output</span>
      </ReportTile>
    );
  }
  return (
    <ReportTile icon={FileDiff} title="Patch" status={patch.status}>
      {patch.files.length ? (
        <ul className="run-report-files">
          {patch.files.slice(0, 8).map((file, index) => (
            <li key={`${file.path}-${index}`}>
              <StatusBadge value={file.status} />
              <span className="mono-text">{file.path}</span>
              <em>{file.operation}</em>
              {file.error ? <strong>{file.error}</strong> : file.summary ? <small>{file.summary}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <span>{patch.status.replaceAll("_", " ")}</span>
      )}
    </ReportTile>
  );
}

function VerificationView({ report }: { report: RunReport }) {
  const verification = report.verification;
  return (
    <ReportTile icon={CheckCircle2} title="Verification" status={verification.status}>
      {verification.commands.length ? (
        <ul className="run-report-commands">
          {verification.commands.slice(0, 6).map((command, index) => (
            <li key={`${command.command}-${index}`}>
              <StatusBadge value={command.status} />
              <span className="mono-text">{command.command}</span>
              {command.summary ? <small>{command.summary}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <span>{verification.reason || verification.status.replaceAll("_", " ")}</span>
      )}
    </ReportTile>
  );
}

function RoleOutputsView({ report, compact }: { report: RunReport; compact: boolean }) {
  return (
    <section className="run-report-section">
      <h3>
        <Wrench size={15} aria-hidden />
        Agent output
      </h3>
      <div className="run-report-role-list">
        {(compact ? report.role_outputs.slice(-2) : report.role_outputs).map((output) => (
          <article key={output.role} className="run-report-role">
            <StatusBadge value={output.role} />
            <p>{output.summary}</p>
            {output.tool_count ? <small>{output.tool_count} tool calls</small> : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function ToolActivityView({ report, compact }: { report: RunReport; compact: boolean }) {
  return (
    <section className="run-report-section">
      <h3>
        <TerminalSquare size={15} aria-hidden />
        Tool activity
      </h3>
      <div className="run-report-tools">
        {(compact ? report.tool_activity.slice(-4) : report.tool_activity).map((tool, index) => (
          <div key={`${tool.tool_name}-${index}`} className="run-report-tool">
            <StatusBadge value={tool.status} />
            <span>{tool.title}</span>
            {tool.target ? <em>{tool.target}</em> : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function IssueStrip({ title, items, tone }: { title: string; items: string[]; tone: "warning" | "danger" }) {
  return (
    <section className={`run-report-issues ${tone}`}>
      <h3>
        <AlertTriangle size={15} aria-hidden />
        {title}
      </h3>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

function ReportTile({
  icon: Icon,
  title,
  status,
  children,
}: {
  icon: LucideIcon;
  title: string;
  status: string;
  children: ReactNode;
}) {
  return (
    <section className="run-report-tile">
      <div className="run-report-tile-head">
        <h3>
          <Icon size={15} aria-hidden />
          {title}
        </h3>
        <StatusBadge value={status} />
      </div>
      {children}
    </section>
  );
}

function stringOr(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}
