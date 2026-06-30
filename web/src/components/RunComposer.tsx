"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { createRun, listAgentGraphs, listModelProfiles } from "@/lib/api";
import type { RunMode } from "@/types";

export default function RunComposer() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [task, setTask] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [profileId, setProfileId] = useState("");
  const [graphId, setGraphId] = useState("");
  const [mode, setMode] = useState<RunMode>("general");
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles });
  const graphsQuery = useQuery({ queryKey: ["agent-graphs"], queryFn: listAgentGraphs });
  const profiles = profilesQuery.data ?? [];
  const graphs = graphsQuery.data ?? [];

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      setTask("");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      router.push(`/runs/${run.id}`);
    },
  });

  useEffect(() => {
    if (!profileId && profiles.length) {
      setProfileId(profiles.find((profile) => profile.enabled)?.id ?? profiles[0].id);
    }
    if (!graphId && graphs.length) {
      setGraphId(graphs.find((graph) => graph.is_default)?.id ?? graphs[0].id);
    }
  }, [graphId, graphs, profileId, profiles]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!task.trim()) {
      return;
    }
    mutation.mutate({
      task: task.trim(),
      workspace: workspace.trim() || null,
      model_provider: null,
      default_model_profile_id: profileId || null,
      agent_graph_id: graphId || null,
      mode,
    });
  };

  return (
    <form className="composer-shell" onSubmit={submit}>
      <label className="field field-task">
        <span>Task</span>
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="Describe the coding, analysis, research, or database task"
          rows={7}
        />
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
        <input value={workspace} onChange={(event) => setWorkspace(event.target.value)} placeholder="/workspace/project" />
      </label>
      {profilesQuery.error ? <div className="error-line">{profilesQuery.error.message}</div> : null}
      {graphsQuery.error ? <div className="error-line">{graphsQuery.error.message}</div> : null}
      {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
      <button className="primary-button" type="submit" disabled={mutation.isPending || !task.trim()}>
        {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Play size={16} aria-hidden />}
        <span>Run</span>
      </button>
    </form>
  );
}
