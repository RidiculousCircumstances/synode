import type { LucideIcon } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

type StatusTone = "neutral" | "success" | "warning" | "danger" | "info";

const STATUS_TONE_BY_VALUE: Record<string, StatusTone> = {
  approved: "success",
  completed: "success",
  healthy: "success",
  ok: "success",
  ready: "success",
  rejected: "danger",
  failed: "danger",
  failed_verification: "danger",
  error: "danger",
  warning: "warning",
  waiting_approval: "warning",
  waiting_operator: "warning",
  pending: "warning",
  queued: "info",
  cancelling: "warning",
  cancelled: "neutral",
  created: "neutral",
  idle: "neutral",
  running: "info",
  started: "info",
};

const STATUS_CLASS: Record<StatusTone, string> = {
  neutral: "status-neutral",
  success: "status-success",
  warning: "status-warning",
  danger: "status-danger",
  info: "status-info",
};

export function StatusBadge({
  value,
  tone,
  children,
  className,
}: {
  value: string;
  tone?: StatusTone;
  children?: ReactNode;
  className?: string;
}) {
  const normalized = value.trim().toLowerCase();
  const normalizedTone = tone ?? STATUS_TONE_BY_VALUE[normalized] ?? "neutral";
  return (
    <span className={cn("status-badge", STATUS_CLASS[normalizedTone], className)} title={value}>
      <span>{children ?? value.replaceAll("_", " ")}</span>
    </span>
  );
}

export function Panel({
  title,
  action,
  children,
  className,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("panel", className)}>
      <div className="panel-header">
        <h2>{title}</h2>
        {action ? <div className="panel-action">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  icon: Icon,
  summary,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  icon: LucideIcon;
  summary?: ReactNode;
}) {
  return (
    <header className="page-cockpit">
      <div className="page-title">
        <span className="page-icon">
          <Icon size={20} aria-hidden />
        </span>
        <div className="min-w-0">
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h1>{title}</h1>
          {description ? <p className="page-description">{description}</p> : null}
        </div>
      </div>
      {summary ? <div className="page-summary">{summary}</div> : null}
    </header>
  );
}

export function PageTabs<T extends string>({
  active,
  items,
  onChange,
  ariaLabel,
}: {
  active: T;
  items: Array<{ id: T; label: string; description?: string; icon: LucideIcon; count?: number | string }>;
  onChange: (id: T) => void;
  ariaLabel: string;
}) {
  return (
    <nav className="page-tabs" aria-label={ariaLabel}>
      {items.map((item) => {
        const Icon = item.icon;
        const selected = active === item.id;
        return (
          <button
            key={item.id}
            type="button"
            className={cn("page-tab", selected && "selected")}
            aria-current={selected ? "page" : undefined}
            onClick={() => onChange(item.id)}
          >
            <span className="page-tab-icon">
              <Icon size={17} aria-hidden />
            </span>
            <span className="page-tab-copy">
              <span className="page-tab-label">
                {item.label}
                {item.count !== undefined ? <em>{item.count}</em> : null}
              </span>
              {item.description ? <span className="page-tab-description">{item.description}</span> : null}
            </span>
          </button>
        );
      })}
    </nav>
  );
}

export function MetricTile({
  label,
  value,
  icon: Icon,
  tone,
}: {
  label: string;
  value: ReactNode;
  icon?: LucideIcon;
  tone?: "normal" | "warning" | "danger";
}) {
  return (
    <div className={cn("metric-tile", tone && `metric-${tone}`)}>
      <div className="metric-label">
        {Icon ? <Icon size={15} aria-hidden /> : null}
        <span>{label}</span>
      </div>
      <strong>{value}</strong>
    </div>
  );
}

export function CompactList({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("compact-list", className)}>{children}</div>;
}

export function CompactRow({
  children,
  selected,
  className,
}: {
  children: ReactNode;
  selected?: boolean;
  className?: string;
}) {
  return <div className={cn("compact-row", selected && "selected", className)}>{children}</div>;
}

export function CompactTableShell({
  children,
  className,
  minWidth = "56rem",
  maxHeight,
}: {
  children: ReactNode;
  className?: string;
  minWidth?: string;
  maxHeight?: string;
}) {
  return (
    <div className={cn("compact-table-shell", className)} style={{ "--compact-table-max-height": maxHeight } as CSSProperties}>
      <div className="compact-table-scroll" style={{ minWidth }}>
        {children}
      </div>
    </div>
  );
}

export function CompactTable({ children, className }: { children: ReactNode; className?: string }) {
  return <table className={cn("compact-table", className)}>{children}</table>;
}

export function EmptyState({ title, text }: { title: string; text?: string }) {
  return (
    <div className="empty-state" role="status">
      <strong>{title}</strong>
      {text ? <span>{text}</span> : null}
    </div>
  );
}

export function CodeBlock({ value, className }: { value: string; className?: string }) {
  return <pre className={cn("code-block", className)}>{value || "empty"}</pre>;
}
