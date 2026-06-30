"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Power, PowerOff, RefreshCw, Settings, X } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { CompactList, CompactRow, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import {
  clearApiBaseUrlCache,
  createModelProfile,
  createSecret,
  getApiBaseUrl,
  getModelHealth,
  listModelProfiles,
  listSecrets,
  testModelProfile,
  updateModelProfile,
} from "@/lib/api";
import type { ModelProfile, ModelProfileTestResult, ModelProviderType } from "@/types";

type ProfileFormMode = "create" | "edit";

type ProfileFormState = {
  id: string | null;
  name: string;
  providerType: ModelProviderType;
  baseUrl: string;
  model: string;
  secretId: string;
  optionsText: string;
  enabled: boolean;
};

const EMPTY_PROFILE_FORM: ProfileFormState = {
  id: null,
  name: "",
  providerType: "ollama",
  baseUrl: "",
  model: "qwen2.5-coder:7b",
  secretId: "",
  optionsText: "{}",
  enabled: true,
};

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
  const [profileForm, setProfileForm] = useState<ProfileFormState>(EMPTY_PROFILE_FORM);
  const [profileDialogMode, setProfileDialogMode] = useState<ProfileFormMode>("create");
  const [profileDialogOpen, setProfileDialogOpen] = useState(false);
  const [secretDialogOpen, setSecretDialogOpen] = useState(false);
  const [profileTestResults, setProfileTestResults] = useState<Record<string, ModelProfileTestResult>>({});
  const createParamHandled = useRef(false);

  const optionsParse = useMemo(() => parseOptionsJson(profileForm.optionsText), [profileForm.optionsText]);
  const providerRequiresBaseUrl = profileForm.providerType === "openai_compatible";
  const profileFormValid =
    Boolean(profileForm.name.trim()) &&
    Boolean(profileForm.model.trim()) &&
    optionsParse.ok &&
    (!providerRequiresBaseUrl || Boolean(profileForm.baseUrl.trim()));

  const resetSecretForm = () => {
    setSecretName("");
    setSecretValue("");
  };

  const closeProfileDialog = () => {
    setProfileDialogOpen(false);
    setProfileDialogMode("create");
    setProfileForm(EMPTY_PROFILE_FORM);
  };

  const openCreateProfileDialog = () => {
    setProfileDialogMode("create");
    setProfileForm(EMPTY_PROFILE_FORM);
    setProfileDialogOpen(true);
  };

  useEffect(() => {
    if (createParamHandled.current || typeof window === "undefined") {
      return;
    }
    if (new URLSearchParams(window.location.search).get("create") === "model-profile") {
      createParamHandled.current = true;
      openCreateProfileDialog();
    }
  }, []);

  const openEditProfileDialog = (profile: ModelProfile) => {
    setProfileDialogMode("edit");
    setProfileForm({
      id: profile.id,
      name: profile.name,
      providerType: profile.provider_type,
      baseUrl: profile.base_url ?? "",
      model: profile.model,
      secretId: profile.secret_id ?? "",
      optionsText: JSON.stringify(profile.options ?? {}, null, 2),
      enabled: profile.enabled,
    });
    setProfileDialogOpen(true);
  };

  const secretMutation = useMutation({
    mutationFn: createSecret,
    onSuccess: () => {
      resetSecretForm();
      setSecretDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });
  const profileMutation = useMutation({
    mutationFn: async () => {
      if (!optionsParse.ok) {
        throw new Error(optionsParse.error);
      }
      const payload = {
        name: profileForm.name.trim(),
        provider_type: profileForm.providerType,
        base_url: profileForm.baseUrl.trim() || null,
        model: profileForm.model.trim(),
        secret_id: profileForm.secretId || null,
        options: optionsParse.value,
        enabled: profileForm.enabled,
      };
      if (profileDialogMode === "edit" && profileForm.id) {
        return updateModelProfile(profileForm.id, payload);
      }
      return createModelProfile(payload);
    },
    onSuccess: () => {
      closeProfileDialog();
      void queryClient.invalidateQueries({ queryKey: ["model-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["model-health"] });
    },
  });
  const toggleProfileMutation = useMutation({
    mutationFn: ({ profileId, enabled }: { profileId: string; enabled: boolean }) =>
      updateModelProfile(profileId, { enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["model-profiles"] });
      void queryClient.invalidateQueries({ queryKey: ["model-health"] });
    },
  });
  const testProfileMutation = useMutation({
    mutationFn: testModelProfile,
    onSuccess: (result) => {
      setProfileTestResults((current) => ({ ...current, [result.profile_id]: result }));
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
    if (!profileFormValid) {
      return;
    }
    profileMutation.mutate();
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
            <div className="panel-action-cluster">
              <button
                type="button"
                className="secondary-button"
                onClick={() => void profilesQuery.refetch()}
              >
                <RefreshCw size={15} aria-hidden />
                Refresh
              </button>
              <button type="button" className="primary-button" onClick={openCreateProfileDialog}>
                <Plus size={15} aria-hidden />
                New profile
              </button>
            </div>
          }
        >
          <CompactList>
            {(profilesQuery.data ?? []).map((profile) => {
              const result = profileTestResults[profile.id];
              return (
                <CompactRow key={profile.id} className="model-profile-row">
                  <div className="profile-copy">
                    <strong>{profile.name}</strong>
                    <em>{profile.provider_type}</em>
                  </div>
                  <StatusBadge value={profile.enabled ? "enabled" : "disabled"} />
                  <span>{profile.model}</span>
                  <span>{profile.base_url ?? "default endpoint"}</span>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="secondary-button compact-control"
                      onClick={() => testProfileMutation.mutate(profile.id)}
                      disabled={testProfileMutation.isPending}
                    >
                      <RefreshCw size={14} aria-hidden className={testProfileMutation.isPending ? "spin" : undefined} />
                      Test
                    </button>
                    <button
                      type="button"
                      className="secondary-button compact-control"
                      onClick={() => openEditProfileDialog(profile)}
                    >
                      <Pencil size={14} aria-hidden />
                      Edit
                    </button>
                    <button
                      type="button"
                      className="secondary-button compact-control"
                      onClick={() => toggleProfileMutation.mutate({ profileId: profile.id, enabled: !profile.enabled })}
                      disabled={toggleProfileMutation.isPending}
                    >
                      {profile.enabled ? <PowerOff size={14} aria-hidden /> : <Power size={14} aria-hidden />}
                      {profile.enabled ? "Disable" : "Enable"}
                    </button>
                  </div>
                  {result ? <ProfileTestResultView result={result} /> : null}
                </CompactRow>
              );
            })}
          </CompactList>
          {profilesQuery.error ? <div className="error-line">{profilesQuery.error.message}</div> : null}
          {testProfileMutation.error ? <div className="error-line">{testProfileMutation.error.message}</div> : null}
        </Panel>
        <Panel
          title="Secrets"
          action={
            <button type="button" className="primary-button" onClick={() => setSecretDialogOpen(true)}>
              <Plus size={15} aria-hidden />
              New secret
            </button>
          }
        >
          <CompactList>
            {(secretsQuery.data ?? []).map((secret) => (
              <CompactRow key={secret.id} className="provider-row">
                <strong>{secret.name}</strong>
                <StatusBadge value={secret.secret_set ? "set" : "empty"} />
                <span>{new Date(secret.updated_at).toLocaleString()}</span>
              </CompactRow>
            ))}
          </CompactList>
          {secretsQuery.error ? <div className="error-line">{secretsQuery.error.message}</div> : null}
        </Panel>
      </div>
      {profileDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="profile-title">
          <button type="button" className="modal-backdrop" aria-label="Close dialog" onClick={closeProfileDialog} />
          <section className="modal-panel">
            <header className="modal-header">
              <h2 id="profile-title">{profileDialogMode === "edit" ? "Edit model profile" : "New model profile"}</h2>
              <button type="button" className="icon-button" aria-label="Close dialog" onClick={closeProfileDialog}>
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitProfile}>
              <div className="form-grid">
                <label className="field">
                  <span>Name</span>
                  <input
                    value={profileForm.name}
                    onChange={(event) => setProfileForm({ ...profileForm, name: event.target.value })}
                  />
                </label>
                <label className="field">
                  <span>Provider</span>
                  <select
                    value={profileForm.providerType}
                    onChange={(event) =>
                      setProfileForm({ ...profileForm, providerType: event.target.value as ModelProviderType })
                    }
                  >
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
                    value={profileForm.baseUrl}
                    onChange={(event) => setProfileForm({ ...profileForm, baseUrl: event.target.value })}
                    placeholder={profileForm.providerType === "openai_compatible" ? "http://127.0.0.1:8000" : "default"}
                  />
                </label>
                <label className="field">
                  <span>Model</span>
                  <input
                    value={profileForm.model}
                    onChange={(event) => setProfileForm({ ...profileForm, model: event.target.value })}
                  />
                </label>
              </div>
              <div className="form-grid">
                <label className="field">
                  <span>Secret</span>
                  <select
                    value={profileForm.secretId}
                    onChange={(event) => setProfileForm({ ...profileForm, secretId: event.target.value })}
                  >
                    <option value="">none</option>
                    {(secretsQuery.data ?? []).map((secret) => (
                      <option key={secret.id} value={secret.id}>
                        {secret.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Enabled</span>
                  <select
                    value={profileForm.enabled ? "true" : "false"}
                    onChange={(event) => setProfileForm({ ...profileForm, enabled: event.target.value === "true" })}
                  >
                    <option value="true">enabled</option>
                    <option value="false">disabled</option>
                  </select>
                </label>
              </div>
              <label className="field">
                <span>Options JSON</span>
                <textarea
                  value={profileForm.optionsText}
                  onChange={(event) => setProfileForm({ ...profileForm, optionsText: event.target.value })}
                  rows={5}
                />
              </label>
              {!optionsParse.ok ? <div className="error-line">{optionsParse.error}</div> : null}
              {providerRequiresBaseUrl && !profileForm.baseUrl.trim() ? (
                <div className="error-line">openai compatible profiles require base URL</div>
              ) : null}
              {profileMutation.error ? <div className="error-line">{profileMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button type="button" className="secondary-button" onClick={closeProfileDialog} disabled={profileMutation.isPending}>
                  Cancel
                </button>
                <button className="primary-button" type="submit" disabled={profileMutation.isPending || !profileFormValid}>
                  {profileMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  {profileDialogMode === "edit" ? "Save profile" : "Create profile"}
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
      {secretDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="new-secret-title">
          <button
            type="button"
            className="modal-backdrop"
            aria-label="Close dialog"
            onClick={() => {
              resetSecretForm();
              setSecretDialogOpen(false);
            }}
          />
          <section className="modal-panel modal-panel-narrow">
            <header className="modal-header">
              <h2 id="new-secret-title">New secret</h2>
              <button
                type="button"
                className="icon-button"
                aria-label="Close dialog"
                onClick={() => {
                  resetSecretForm();
                  setSecretDialogOpen(false);
                }}
              >
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitSecret}>
              <label className="field">
                <span>Name</span>
                <input value={secretName} onChange={(event) => setSecretName(event.target.value)} />
              </label>
              <label className="field">
                <span>Value</span>
                <input type="password" value={secretValue} onChange={(event) => setSecretValue(event.target.value)} />
              </label>
              {secretMutation.error ? <div className="error-line">{secretMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    resetSecretForm();
                    setSecretDialogOpen(false);
                  }}
                  disabled={secretMutation.isPending}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={secretMutation.isPending || !secretName.trim() || !secretValue.trim()}
                >
                  {secretMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  Create secret
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function ProfileTestResultView({ result }: { result: ModelProfileTestResult }) {
  return (
    <div className="profile-test-result">
      <StatusBadge value={result.ok ? "ok" : "error"}>{result.ok ? "test ok" : "test failed"}</StatusBadge>
      {result.checks.map((check) => (
        <span key={check.name} className="test-check-chip">
          {check.name.replaceAll("_", " ")}: {check.supported ? (check.ok ? "ok" : check.error ?? "failed") : "unsupported"}
        </span>
      ))}
    </div>
  );
}

function parseOptionsJson(value: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  try {
    const parsed = JSON.parse(value || "{}") as unknown;
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      return { ok: false, error: "Options JSON must be an object" };
    }
    return { ok: true, value: parsed as Record<string, unknown> };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Invalid options JSON" };
  }
}
