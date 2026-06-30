"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Settings } from "lucide-react";

import { CompactList, CompactRow, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import { clearApiBaseUrlCache, getApiBaseUrl, getModelHealth } from "@/lib/api";

export default function SettingsPage() {
  const apiQuery = useQuery({
    queryKey: ["ui-api-base-url"],
    queryFn: getApiBaseUrl,
    staleTime: Infinity,
  });
  const modelsQuery = useQuery({ queryKey: ["model-health"], queryFn: getModelHealth, refetchInterval: 10000 });

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="configuration"
        title="Settings"
        description="Runtime API resolution and provider diagnostics."
        icon={Settings}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="API" value={apiQuery.data ?? "resolving"} />
            <MetricTile label="Providers" value={modelsQuery.data?.length ?? 0} />
          </div>
        }
      />
      <Panel
        title="UI runtime config"
        action={
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              clearApiBaseUrlCache();
              void apiQuery.refetch();
              void modelsQuery.refetch();
            }}
          >
            <RefreshCw size={15} aria-hidden />
            Refresh
          </button>
        }
      >
        <div className="settings-grid">
          <MetricTile label="Resolved API base URL" value={apiQuery.data ?? "n/a"} />
          <MetricTile label="Config source" value="runtime / browser host" />
        </div>
        {apiQuery.error ? <div className="error-line">{apiQuery.error.message}</div> : null}
      </Panel>
      <Panel title="Providers">
        <CompactList>
          {(modelsQuery.data ?? []).map((model) => (
            <CompactRow key={model.provider} className="provider-row">
              <strong>{model.provider}</strong>
              <StatusBadge value={model.ok ? "ok" : "error"}>{model.ok ? "ok" : "error"}</StatusBadge>
              <span>{model.model ?? "n/a"}</span>
              <em>{model.error ?? ""}</em>
            </CompactRow>
          ))}
        </CompactList>
        {modelsQuery.error ? <div className="error-line">{modelsQuery.error.message}</div> : null}
      </Panel>
    </div>
  );
}
