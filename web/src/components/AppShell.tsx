"use client";

import {
  Activity,
  Bot,
  GitBranch,
  Menu,
  MessageSquare,
  Settings,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { getApiBaseUrl, getModelHealth, getSystemMetrics } from "@/lib/api";
import { asPercent, formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { StatusBadge } from "@/components/ui/primitives";

type NavItem = {
  href: string;
  label: string;
  description: string;
  icon: LucideIcon;
};

const NAV_ITEMS: NavItem[] = [
  { href: "/threads", label: "Threads", description: "work queue", icon: MessageSquare },
  { href: "/runs", label: "Runs", description: "runtime", icon: GitBranch },
  { href: "/agents", label: "Agents", description: "nodes", icon: Bot },
  { href: "/observability", label: "Observability", description: "metrics", icon: Activity },
  { href: "/settings", label: "Settings", description: "providers", icon: Settings },
];

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const apiBaseUrlQuery = useQuery({
    queryKey: ["ui-api-base-url"],
    queryFn: getApiBaseUrl,
    staleTime: Infinity,
  });
  const modelHealthQuery = useQuery({
    queryKey: ["model-health"],
    queryFn: getModelHealth,
    refetchInterval: 10000,
  });
  const systemMetricsQuery = useQuery({
    queryKey: ["system-metrics"],
    queryFn: getSystemMetrics,
    refetchInterval: 4000,
  });

  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  useBodyScrollLock(mobileOpen);

  const modelOk = modelHealthQuery.data?.every((model) => model.ok) ?? false;
  const memory = formatBytes(systemMetricsQuery.data?.process.memory_rss_bytes);

  return (
    <>
      <div className="app-shell">
        <aside className="app-sidebar-frame">
          <Sidebar pathname={pathname} />
        </aside>
        <div className="app-main-column">
          <header className="app-header">
            <button
              type="button"
              className="mobile-menu-button"
              aria-label="Open navigation"
              onClick={() => setMobileOpen(true)}
            >
              <Menu size={18} aria-hidden />
            </button>
            <div className="header-title">
              <span>Synode</span>
              <code>{apiBaseUrlQuery.data ?? "resolving api"}</code>
            </div>
            <div className="header-signals">
              <StatusBadge value={modelOk ? "ok" : "warning"}>{modelOk ? "models ok" : "models"}</StatusBadge>
              <span className="signal-pill">CPU {asPercent(systemMetricsQuery.data?.process.cpu_percent)}</span>
              <span className="signal-pill">RAM {memory}</span>
            </div>
          </header>
          <main id="main-content" className="app-content">
            {children}
          </main>
        </div>
      </div>
      {mobileOpen ? (
        <div className="mobile-nav" role="dialog" aria-modal="true">
          <button className="mobile-nav-backdrop" type="button" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />
          <div className="mobile-nav-panel">
            <button className="mobile-close-button" type="button" aria-label="Close navigation" onClick={() => setMobileOpen(false)}>
              <X size={18} aria-hidden />
            </button>
            <Sidebar pathname={pathname} onNavigate={() => setMobileOpen(false)} />
          </div>
        </div>
      ) : null}
    </>
  );
}

function Sidebar({
  pathname,
  onNavigate,
}: {
  pathname: string;
  onNavigate?: () => void;
}) {
  return (
    <aside className="app-sidebar">
      <Link href="/threads" className="brand" onClick={onNavigate}>
        <span className="brand-mark">S</span>
        <span>
          <strong>Synode</strong>
          <em>agent runtime</em>
        </span>
      </Link>
      <nav className="sidebar-nav" aria-label="Main navigation">
        <div className="sidebar-group-label">
          <span>Workspace</span>
        </div>
        {NAV_ITEMS.map((item) => {
          const active =
            item.href === "/threads"
              ? pathname === "/" || pathname.startsWith("/threads") || pathname.startsWith("/chat")
              : pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              className={cn("sidebar-link", active && "active")}
            >
              <span className="sidebar-icon">
                <Icon size={17} aria-hidden />
              </span>
              <span className="sidebar-copy">
                <span>{item.label}</span>
                <em>{item.description}</em>
              </span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
