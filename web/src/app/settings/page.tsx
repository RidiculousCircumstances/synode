"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, RefreshCw, Settings } from "lucide-react";
import { type FormEvent, useState } from "react";

import { CompactList, CompactRow, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import {
  clearApiBaseUrlCache,
  createModelProfile,
  createSecret,
  getApiBaseUrl,
  getModelHealth,
  listModelProfiles,
  listSecrets,
} from "@/lib/api";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const apiQuery = useQuery({
    queryKey: ["ui-api-base-url"],
    queryFn: getApiBaseUrl,
    staleTime: Infinity,
  });
  const modelsQuery = useQuery({ queryKey: ["model-health"], queryFn: getModelHealth, refetchInterval: 10000 });
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles });
  const secretsQuery = useQuery({ queryKey: ["secrets"], queryFn: listSecrets });
  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [profileName, setProfileName] = useState("");
  const [providerType, setProviderType] = useState("ollama");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("qwen2.5-coder:7b");
  const [secretId, setSecretId] = useState("");
  const secretMutation = useMutation({
    mutationFn: createSecret,
    onSuccess: () => {
      setSecretName("");
      setSecretValue("");
      void queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });
  const profileMutation = useMutation({
    mutationFn: createModelProfile,
    onSuccess: () => {
      setProfileName("");
      setBaseUrl("");
      setSecretId("");
      void queryClient.invalidateQueries({ queryKey: ["model-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["model-health"] });
    },
  });

  const submitSecret = (event: FormEvent) => {
    event.preventDefault();
    if (!secretName.trim() || !secretValue.trim()) {
      return;
    }
    secretMutation.mutate({ name: secretName.trim(), value: secretValue });
  };

  const submitProfile = (event: FormEvent) => {
    event.preventDefault();
    if (!profileName.trim() || !model.trim()) {
      return;
    }
    profileMutation.mutate({
      name: profileName.trim(),
      provider_type: providerType,
      base_url: baseUrl.trim() || null,
      model: model.trim(),
      secret_id: secretId || null,
      options: {},
      enabled: true,
    });
  };

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
            <CompactRow key={model.profile_id ?? model.provider} className="provider-row">
              <strong>{model.profile_name ?? model.provider}</strong>
              <StatusBadge value={model.ok ? "ok" : "error"}>{model.ok ? "ok" : "error"}</StatusBadge>
              <span>{model.provider_type ?? model.provider}</span>
              <span>{model.model ?? "n/a"}</span>
              <em>{model.error ?? ""}</em>
            </CompactRow>
          ))}
        </CompactList>
        {modelsQuery.error ? <div className="error-line">{modelsQuery.error.message}</div> : null}
      </Panel>
      <div className="settings-config-grid">
        <Panel
          title="Model profiles"
          action={
            <button
              type="button"
              className="secondary-button"
              onClick={() => void profilesQuery.refetch()}
            >
              <RefreshCw size={15} aria-hidden />
              Refresh
            </button>
          }
        >
          <CompactList>
            {(profilesQuery.data ?? []).map((profile) => (
              <CompactRow key={profile.id} className="provider-row">
                <strong>{profile.name}</strong>
                <StatusBadge value={profile.enabled ? "enabled" : "disabled"} />
                <span>{profile.provider_type}</span>
                <span>{profile.model}</span>
              </CompactRow>
            ))}
          </CompactList>
          <form className="inline-config-form" onSubmit={submitProfile}>
            <div className="form-grid">
              <label className="field">
                <span>Name</span>
                <input value={profileName} onChange={(event) => setProfileName(event.target.value)} />
              </label>
              <label className="field">
                <span>Provider</span>
                <select value={providerType} onChange={(event) => setProviderType(event.target.value)}>
                  <option value="ollama">ollama</option>
                  <option value="openai_compatible">openai compatible</option>
                  <option value="fake">fake</option>
                </select>
              </label>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>Base URL</span>
                <input
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="http://127.0.0.1:11434"
                />
              </label>
              <label className="field">
                <span>Model</span>
                <input value={model} onChange={(event) => setModel(event.target.value)} />
              </label>
            </div>
            <label className="field">
              <span>Secret</span>
              <select value={secretId} onChange={(event) => setSecretId(event.target.value)}>
                <option value="">none</option>
                {(secretsQuery.data ?? []).map((secret) => (
                  <option key={secret.id} value={secret.id}>
                    {secret.name}
                  </option>
                ))}
              </select>
            </label>
            {profileMutation.error ? <div className="error-line">{profileMutation.error.message}</div> : null}
            <button className="primary-button" type="submit" disabled={profileMutation.isPending}>
              <Plus size={15} aria-hidden />
              Create profile
            </button>
          </form>
        </Panel>
        <Panel title="Secrets" action={<KeyRound size={16} aria-hidden />}>
          <CompactList>
            {(secretsQuery.data ?? []).map((secret) => (
              <CompactRow key={secret.id} className="provider-row">
                <strong>{secret.name}</strong>
                <StatusBadge value={secret.secret_set ? "set" : "empty"} />
                <span>{new Date(secret.updated_at).toLocaleString()}</span>
              </CompactRow>
            ))}
          </CompactList>
          <form className="inline-config-form" onSubmit={submitSecret}>
            <label className="field">
              <span>Name</span>
              <input value={secretName} onChange={(event) => setSecretName(event.target.value)} />
            </label>
            <label className="field">
              <span>Value</span>
              <input
                type="password"
                value={secretValue}
                onChange={(event) => setSecretValue(event.target.value)}
              />
            </label>
            {secretMutation.error ? <div className="error-line">{secretMutation.error.message}</div> : null}
            <button className="primary-button" type="submit" disabled={secretMutation.isPending}>
              <Plus size={15} aria-hidden />
              Create secret
            </button>
          </form>
        </Panel>
      </div>
    </div>
  );
}
