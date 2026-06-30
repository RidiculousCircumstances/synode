"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { createThread, listAgentGraphs, listModelProfiles } from "@/lib/api";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { RunMode } from "@/types";

export default function NewThreadDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [title, setTitle] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [profileId, setProfileId] = useState("");
  const [graphId, setGraphId] = useState("");
  const [mode, setMode] = useState<RunMode>("general");
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles, enabled: open });
  const graphsQuery = useQuery({ queryKey: ["agent-graphs"], queryFn: listAgentGraphs, enabled: open });
  const profiles = profilesQuery.data ?? [];
  const graphs = graphsQuery.data ?? [];

  const mutation = useMutation({
    mutationFn: createThread,
    onSuccess: (detail) => {
      setMessage("");
      setTitle("");
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      onClose();
      router.push(`/threads/${detail.thread.id}`);
    },
  });

  useBodyScrollLock(open);

  useEffect(() => {
    if (!open) {
      return;
    }
    if (!profileId && profiles.length) {
      setProfileId(profiles.find((profile) => profile.enabled)?.id ?? profiles[0].id);
    }
    if (!graphId && graphs.length) {
      setGraphId(graphs.find((graph) => graph.is_default)?.id ?? graphs[0].id);
    }
  }, [graphId, graphs, open, profileId, profiles]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!message.trim()) {
      return;
    }
    mutation.mutate({
      message: message.trim(),
      title: title.trim() || null,
      workspace: workspace.trim() || null,
      model_provider: null,
      default_model_profile_id: profileId || null,
      agent_graph_id: graphId || null,
      mode,
    });
  };

  if (!open) {
    return null;
  }

  return (
    <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="new-thread-title">
      <button type="button" className="modal-backdrop" aria-label="Close dialog" onClick={onClose} />
      <section className="modal-panel">
        <header className="modal-header">
          <h2 id="new-thread-title">New thread</h2>
          <button type="button" className="icon-button" aria-label="Close dialog" onClick={onClose}>
            <X size={16} aria-hidden />
          </button>
        </header>
        <form className="composer-shell thread-modal-form" onSubmit={submit}>
          <label className="field field-task">
            <span>Message</span>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="Describe the coding, analysis, research, or database task"
              rows={7}
              autoFocus
            />
          </label>
          <label className="field">
            <span>Title</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Optional" />
          </label>
          <div className="form-grid">
            <label className="field">
              <span>Mode</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as RunMode)}>
                <option value="general">general</option>
                <option value="coding">coding</option>
              </select>
            </label>
            <label className="field">
              <div className="field-heading">
                <span>Model profile</span>
                <Link className="field-action-link" href="/settings?create=model-profile">
                  <Plus size={13} aria-hidden />
                  New
                </Link>
              </div>
              <select value={profileId} onChange={(event) => setProfileId(event.target.value)}>
                <option value="">default</option>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="field">
            <div className="field-heading">
              <span>Agent graph</span>
              <Link className="field-action-link" href="/workflows?create=agent-graph">
                <Plus size={13} aria-hidden />
                New
              </Link>
            </div>
            <select value={graphId} onChange={(event) => setGraphId(event.target.value)}>
              <option value="">default</option>
              {graphs.map((graph) => (
                <option key={graph.id} value={graph.id}>
                  {graph.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Workspace</span>
            <input
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder="/workspace/project"
            />
          </label>
          {profilesQuery.error ? <div className="error-line">{profilesQuery.error.message}</div> : null}
          {graphsQuery.error ? <div className="error-line">{graphsQuery.error.message}</div> : null}
          {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
          <footer className="modal-actions">
            <button type="button" className="secondary-button" onClick={onClose}>
              Cancel
            </button>
            <button className="primary-button" type="submit" disabled={mutation.isPending || !message.trim()}>
              {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Play size={16} aria-hidden />}
              <span>Run</span>
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
