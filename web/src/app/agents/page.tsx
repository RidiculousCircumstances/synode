"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, GitBranch, Plus, RefreshCw, X } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";

import AgentGraph from "@/components/AgentGraph";
import { CompactList, CompactRow, MetricTile, PageHeader, Panel, StatusBadge } from "@/components/ui/primitives";
import { useRunEvents } from "@/hooks/useRunEvents";
import { createAgent, createAgentGraph, getRun, listAgentGraphs, listAgents, listModelProfiles, listRuns } from "@/lib/api";

export default function AgentsPage() {
  const queryClient = useQueryClient();
  const agentsQuery = useQuery({ queryKey: ["agents"], queryFn: listAgents });
  const graphsQuery = useQuery({ queryKey: ["agent-graphs"], queryFn: listAgentGraphs });
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles });
  const runsQuery = useQuery({ queryKey: ["runs"], queryFn: listRuns, refetchInterval: 5000 });
  const latestRunId = runsQuery.data?.[0]?.id ?? null;
  const runQuery = useQuery({
    queryKey: ["run", latestRunId],
    queryFn: () => getRun(latestRunId ?? ""),
    enabled: latestRunId !== null,
    refetchInterval: 4000,
  });
  const events = useRunEvents(latestRunId);
  const agents = agentsQuery.data ?? [];
  const graphs = graphsQuery.data ?? [];
  const profiles = profilesQuery.data ?? [];
  const [roleName, setRoleName] = useState("");
  const [roleMission, setRoleMission] = useState("");
  const [roleTools, setRoleTools] = useState("");
  const [graphName, setGraphName] = useState("");
  const [graphRoleIds, setGraphRoleIds] = useState<string[]>([]);
  const [graphProfileId, setGraphProfileId] = useState("");
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [graphDialogOpen, setGraphDialogOpen] = useState(false);
  const selectableRoles = useMemo(
    () => agents.filter((agent) => agent.enabled),
    [agents],
  );
  const resetRoleForm = () => {
    setRoleName("");
    setRoleMission("");
    setRoleTools("");
  };
  const resetGraphForm = () => {
    setGraphName("");
    setGraphRoleIds([]);
    setGraphProfileId("");
  };
  const roleMutation = useMutation({
    mutationFn: createAgent,
    onSuccess: () => {
      resetRoleForm();
      setRoleDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  const graphMutation = useMutation({
    mutationFn: createAgentGraph,
    onSuccess: () => {
      resetGraphForm();
      setGraphDialogOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["agent-graphs"] });
    },
  });

  const submitRole = (event: FormEvent) => {
    event.preventDefault();
    if (!roleName.trim() || !roleMission.trim()) {
      return;
    }
    roleMutation.mutate({
      name: roleName.trim(),
      mission: roleMission.trim(),
      allowed_tools: roleTools.split(",").map((tool) => tool.trim()).filter(Boolean),
      non_goals: [],
      requires_approval_for: [],
      output_contract: "",
      enabled: true,
    });
  };

  const submitGraph = (event: FormEvent) => {
    event.preventDefault();
    const roleIds = graphRoleIds.filter(Boolean);
    if (!graphName.trim() || roleIds.length < 3) {
      return;
    }
    graphMutation.mutate({
      name: graphName.trim(),
      role_ids: roleIds,
      edges: roleIds.slice(0, -1).map((roleId, index) => ({
        from_role: roleId,
        to_role: roleIds[index + 1],
      })),
      default_model_profile_id: graphProfileId || null,
      role_model_profile_ids: {},
      is_default: false,
      enabled: true,
    });
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="nodes"
        title="Agents"
        description="Role inventory and latest run graph."
        icon={Bot}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Roles" value={agents.length} />
            <MetricTile label="Latest run" value={latestRunId ? latestRunId.slice(0, 8) : "n/a"} icon={GitBranch} />
          </div>
        }
      />
      <div className="agents-layout">
        <Panel title="Latest run graph" className="full-height-panel">
          <AgentGraph run={runQuery.data ?? null} events={events} agents={agents} />
        </Panel>
        <Panel
          title="Role catalog"
          action={
            <button type="button" className="primary-button" onClick={() => setRoleDialogOpen(true)}>
              <Plus size={15} aria-hidden />
              New role
            </button>
          }
        >
          <CompactList>
            {agents.map((agent) => (
              <CompactRow key={agent.name} className="agent-catalog-row">
                <StatusBadge value={agent.name} />
                <strong>{agent.mission}</strong>
                <em>{agent.allowed_tools.length} tools</em>
              </CompactRow>
            ))}
          </CompactList>
          {agentsQuery.error ? <div className="error-line">{agentsQuery.error.message}</div> : null}
        </Panel>
        <Panel
          title="Agent graphs"
          action={
            <button type="button" className="primary-button" onClick={() => setGraphDialogOpen(true)}>
              <Plus size={15} aria-hidden />
              New graph
            </button>
          }
        >
          <CompactList>
            {graphs.map((graph) => (
              <CompactRow key={graph.id} className="agent-catalog-row">
                <StatusBadge value={graph.is_default ? "default" : graph.enabled ? "enabled" : "disabled"} />
                <strong>{graph.name}</strong>
                <span>{graph.role_ids.length} roles</span>
                <em>{graph.edges.length} edges</em>
              </CompactRow>
            ))}
          </CompactList>
          {graphsQuery.error ? <div className="error-line">{graphsQuery.error.message}</div> : null}
        </Panel>
      </div>
      {roleDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="new-role-title">
          <button
            type="button"
            className="modal-backdrop"
            aria-label="Close dialog"
            onClick={() => {
              resetRoleForm();
              setRoleDialogOpen(false);
            }}
          />
          <section className="modal-panel">
            <header className="modal-header">
              <h2 id="new-role-title">New role</h2>
              <button
                type="button"
                className="icon-button"
                aria-label="Close dialog"
                onClick={() => {
                  resetRoleForm();
                  setRoleDialogOpen(false);
                }}
              >
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitRole}>
              <div className="form-grid">
                <label className="field">
                  <span>Name</span>
                  <input value={roleName} onChange={(event) => setRoleName(event.target.value)} />
                </label>
                <label className="field">
                  <span>Allowed tools</span>
                  <input
                    value={roleTools}
                    onChange={(event) => setRoleTools(event.target.value)}
                    placeholder="native.fs_read, mcp.*"
                  />
                </label>
              </div>
              <label className="field">
                <span>Mission</span>
                <input value={roleMission} onChange={(event) => setRoleMission(event.target.value)} />
              </label>
              {roleMutation.error ? <div className="error-line">{roleMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    resetRoleForm();
                    setRoleDialogOpen(false);
                  }}
                  disabled={roleMutation.isPending}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={roleMutation.isPending || !roleName.trim() || !roleMission.trim()}
                >
                  {roleMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  Create role
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
      {graphDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="new-graph-title">
          <button
            type="button"
            className="modal-backdrop"
            aria-label="Close dialog"
            onClick={() => {
              resetGraphForm();
              setGraphDialogOpen(false);
            }}
          />
          <section className="modal-panel">
            <header className="modal-header">
              <h2 id="new-graph-title">New graph</h2>
              <button
                type="button"
                className="icon-button"
                aria-label="Close dialog"
                onClick={() => {
                  resetGraphForm();
                  setGraphDialogOpen(false);
                }}
              >
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitGraph}>
              <div className="form-grid">
                <label className="field">
                  <span>Name</span>
                  <input value={graphName} onChange={(event) => setGraphName(event.target.value)} />
                </label>
                <label className="field">
                  <span>Default profile</span>
                  <select value={graphProfileId} onChange={(event) => setGraphProfileId(event.target.value)}>
                    <option value="">none/default</option>
                    {profiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="field">
                <span>Roles</span>
                <select
                  multiple
                  value={graphRoleIds}
                  onChange={(event) =>
                    setGraphRoleIds(Array.from(event.currentTarget.selectedOptions).map((option) => option.value))
                  }
                >
                  {selectableRoles.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
              {graphMutation.error ? <div className="error-line">{graphMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    resetGraphForm();
                    setGraphDialogOpen(false);
                  }}
                  disabled={graphMutation.isPending}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={graphMutation.isPending || !graphName.trim() || graphRoleIds.filter(Boolean).length < 3}
                >
                  {graphMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  Create graph
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}
