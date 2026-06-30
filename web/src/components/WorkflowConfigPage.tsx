"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Bot, Copy, GitBranch, Pencil, Plus, Power, PowerOff, RefreshCw, Star, Users, X } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import AgentGraph from "@/components/AgentGraph";
import {
  CompactTable,
  CompactTableShell,
  MetricTile,
  PageHeader,
  PageTabs,
  Panel,
  StatusBadge,
} from "@/components/ui/primitives";
import { useRunEvents } from "@/hooks/useRunEvents";
import {
  createAgent,
  createAgentGraph,
  getRun,
  listAgentGraphs,
  listAgents,
  listModelProfiles,
  listRuns,
  listTools,
  updateAgent,
  updateAgentGraph,
} from "@/lib/api";
import type { AgentGraph as AgentGraphConfig, AgentSpec } from "@/types";

type RoleFormMode = "create" | "edit";
type GraphFormMode = "create" | "edit" | "clone";
type GraphTemplateId = "coding" | "data_analysis" | "research" | "db_investigation" | "blank";
type WorkflowTab = "workflows" | "roles" | "execution";

type RoleFormState = {
  id: string | null;
  name: string;
  mission: string;
  allowedTools: string;
  approvalTools: string;
  nonGoals: string;
  outputContract: string;
  enabled: boolean;
};

type GraphFormState = {
  id: string | null;
  name: string;
  templateId: GraphTemplateId;
  roleIds: string[];
  workerOrder: string[];
  defaultProfileId: string;
  roleProfileIds: Record<string, string>;
  isDefault: boolean;
  enabled: boolean;
};

const EMPTY_ROLE_FORM: RoleFormState = {
  id: null,
  name: "",
  mission: "",
  allowedTools: "",
  approvalTools: "",
  nonGoals: "",
  outputContract: "",
  enabled: true,
};

const EMPTY_GRAPH_FORM: GraphFormState = {
  id: null,
  name: "",
  templateId: "coding",
  roleIds: [],
  workerOrder: [],
  defaultProfileId: "",
  roleProfileIds: {},
  isDefault: false,
  enabled: true,
};

const GRAPH_TEMPLATES: Record<GraphTemplateId, { label: string; workers: string[] }> = {
  coding: { label: "Coding", workers: ["coder"] },
  data_analysis: { label: "Data analysis", workers: ["data_analyst"] },
  research: { label: "Research", workers: ["web_researcher"] },
  db_investigation: { label: "DB investigation", workers: ["db_agent", "data_analyst"] },
  blank: { label: "Blank", workers: [] },
};

export default function WorkflowConfigPage() {
  const queryClient = useQueryClient();
  const agentsQuery = useQuery({ queryKey: ["agents"], queryFn: listAgents });
  const graphsQuery = useQuery({ queryKey: ["agent-graphs"], queryFn: listAgentGraphs });
  const profilesQuery = useQuery({ queryKey: ["model-profiles"], queryFn: listModelProfiles });
  const toolsQuery = useQuery({ queryKey: ["tools"], queryFn: listTools });
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
  const roleNameById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent.name])), [agents]);
  const profileNameById = useMemo(() => new Map(profiles.map((profile) => [profile.id, profile.name])), [profiles]);
  const createParamHandled = useRef(false);
  const [roleForm, setRoleForm] = useState<RoleFormState>(EMPTY_ROLE_FORM);
  const [roleDialogMode, setRoleDialogMode] = useState<RoleFormMode>("create");
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [graphForm, setGraphForm] = useState<GraphFormState>(EMPTY_GRAPH_FORM);
  const [graphDialogMode, setGraphDialogMode] = useState<GraphFormMode>("create");
  const [graphDialogOpen, setGraphDialogOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<WorkflowTab>("workflows");

  const graphValidation = validateGraphForm(graphForm, agents);
  const graphEdges = buildGraphEdges(graphForm, agents);
  const graphRoleModelBindings = compactBindings(graphForm.roleProfileIds);

  const closeRoleDialog = () => {
    setRoleDialogOpen(false);
    setRoleDialogMode("create");
    setRoleForm(EMPTY_ROLE_FORM);
  };
  const closeGraphDialog = () => {
    setGraphDialogOpen(false);
    setGraphDialogMode("create");
    setGraphForm(EMPTY_GRAPH_FORM);
  };

  const openCreateRoleDialog = () => {
    setRoleDialogMode("create");
    setRoleForm(EMPTY_ROLE_FORM);
    setRoleDialogOpen(true);
  };
  const openEditRoleDialog = (role: AgentSpec) => {
    setRoleDialogMode("edit");
    setRoleForm({
      id: role.id,
      name: role.name,
      mission: role.mission,
      allowedTools: role.allowed_tools.join(", "),
      approvalTools: role.requires_approval_for.join(", "),
      nonGoals: role.non_goals.join("\n"),
      outputContract: role.output_contract,
      enabled: role.enabled,
    });
    setRoleDialogOpen(true);
  };

  const openCreateGraphDialog = () => {
    setGraphDialogMode("create");
    setGraphForm(applyTemplate({ ...EMPTY_GRAPH_FORM, name: "Coding graph" }, "coding", agents));
    setGraphDialogOpen(true);
  };
  const openEditGraphDialog = (graph: AgentGraphConfig) => {
    setGraphDialogMode("edit");
    setGraphForm(graphToForm(graph, agents, "edit"));
    setGraphDialogOpen(true);
  };
  const openCloneGraphDialog = (graph: AgentGraphConfig) => {
    setGraphDialogMode("clone");
    setGraphForm(graphToForm(graph, agents, "clone"));
    setGraphDialogOpen(true);
  };

  useEffect(() => {
    if (createParamHandled.current || typeof window === "undefined") {
      return;
    }
    const createParam = new URLSearchParams(window.location.search).get("create");
    if (createParam === "role") {
      createParamHandled.current = true;
      openCreateRoleDialog();
      return;
    }
    if (createParam === "agent-graph" && agents.length) {
      createParamHandled.current = true;
      openCreateGraphDialog();
    }
  }, [agents.length]);

  const roleMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        mission: roleForm.mission.trim(),
        non_goals: splitLines(roleForm.nonGoals),
        allowed_tools: splitComma(roleForm.allowedTools),
        requires_approval_for: splitComma(roleForm.approvalTools),
        output_contract: roleForm.outputContract,
        enabled: roleForm.enabled,
      };
      if (roleDialogMode === "edit" && roleForm.id) {
        return updateAgent(roleForm.id, payload);
      }
      return createAgent({ name: roleForm.name.trim(), ...payload });
    },
    onSuccess: () => {
      closeRoleDialog();
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
  const graphMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        name: graphForm.name.trim(),
        role_ids: graphForm.roleIds,
        edges: graphEdges,
        default_model_profile_id: graphForm.defaultProfileId || null,
        role_model_profile_ids: graphRoleModelBindings,
        is_default: graphForm.isDefault,
        enabled: graphForm.enabled,
      };
      if (graphDialogMode === "edit" && graphForm.id) {
        return updateAgentGraph(graphForm.id, payload);
      }
      return createAgentGraph(payload);
    },
    onSuccess: () => {
      closeGraphDialog();
      void queryClient.invalidateQueries({ queryKey: ["agent-graphs"] });
    },
  });
  const graphQuickMutation = useMutation({
    mutationFn: ({ graphId, payload }: { graphId: string; payload: { is_default?: boolean; enabled?: boolean } }) =>
      updateAgentGraph(graphId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["agent-graphs"] });
    },
  });

  const submitRole = (event: FormEvent) => {
    event.preventDefault();
    if (!roleForm.mission.trim() || (roleDialogMode === "create" && !roleForm.name.trim())) {
      return;
    }
    roleMutation.mutate();
  };

  const submitGraph = (event: FormEvent) => {
    event.preventDefault();
    if (!graphValidation.ok) {
      return;
    }
    graphMutation.mutate();
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="workflow config"
        title="Workflows"
        description="Graph presets, role inventory, and execution diagnostics."
        icon={Bot}
        summary={
          <div className="summary-grid compact">
            <MetricTile label="Graphs" value={graphs.length} icon={GitBranch} />
            <MetricTile label="Roles" value={agents.length} />
          </div>
        }
      />
      <PageTabs
        active={activeTab}
        onChange={setActiveTab}
        ariaLabel="Workflow configuration sections"
        items={[
          { id: "workflows", label: "Workflows", description: "Graph presets", icon: GitBranch, count: graphs.length },
          { id: "roles", label: "Roles", description: "Role catalog", icon: Users, count: agents.length },
          { id: "execution", label: "Execution", description: "Latest graph", icon: Activity, count: runsQuery.data?.length ?? 0 },
        ]}
      />
      {activeTab === "workflows" ? (
        <Panel
          title="Workflows"
          className="workflow-table-panel"
          action={
            <button type="button" className="primary-button" onClick={openCreateGraphDialog}>
              <Plus size={15} aria-hidden />
              New graph
            </button>
          }
        >
          <CompactTableShell minWidth="68rem" maxHeight="64vh">
            <CompactTable>
              <colgroup>
                <col style={{ width: "22%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "24%" }} />
                <col style={{ width: "14%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "20%" }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Path</th>
                  <th>Profile</th>
                  <th>Overrides</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {graphs.map((graph) => (
                  <tr key={graph.id}>
                    <td>
                      <span className="table-primary-cell">
                        <strong>{graph.name}</strong>
                        <em>{new Date(graph.updated_at).toLocaleString()}</em>
                      </span>
                    </td>
                    <td>
                      <StatusBadge value={graph.is_default ? "default" : graph.enabled ? "enabled" : "disabled"} />
                    </td>
                    <td>
                      <span className="table-truncate">
                        {graph.role_ids.map((roleId) => roleNameById.get(roleId) ?? roleId.slice(0, 8)).join(" -> ")}
                      </span>
                    </td>
                    <td>
                      <span className="table-truncate">
                        {graph.default_model_profile_id
                          ? profileNameById.get(graph.default_model_profile_id) ?? "profile"
                          : "runtime default"}
                      </span>
                    </td>
                    <td>{Object.keys(graph.role_model_profile_ids).length}</td>
                    <td>
                      <div className="row-actions">
                        <button type="button" className="secondary-button compact-control" onClick={() => openEditGraphDialog(graph)}>
                          <Pencil size={14} aria-hidden />
                          Edit
                        </button>
                        <button type="button" className="secondary-button compact-control" onClick={() => openCloneGraphDialog(graph)}>
                          <Copy size={14} aria-hidden />
                          Clone
                        </button>
                        <button
                          type="button"
                          className="secondary-button compact-control"
                          onClick={() => graphQuickMutation.mutate({ graphId: graph.id, payload: { is_default: true } })}
                          disabled={graphQuickMutation.isPending || graph.is_default}
                        >
                          <Star size={14} aria-hidden />
                          Default
                        </button>
                        <button
                          type="button"
                          className="secondary-button compact-control"
                          onClick={() => graphQuickMutation.mutate({ graphId: graph.id, payload: { enabled: !graph.enabled } })}
                          disabled={graphQuickMutation.isPending}
                        >
                          {graph.enabled ? <PowerOff size={14} aria-hidden /> : <Power size={14} aria-hidden />}
                          {graph.enabled ? "Disable" : "Enable"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </CompactTable>
          </CompactTableShell>
          {graphsQuery.error ? <div className="error-line">{graphsQuery.error.message}</div> : null}
          {graphQuickMutation.error ? <div className="error-line">{graphQuickMutation.error.message}</div> : null}
        </Panel>
      ) : null}
      {activeTab === "roles" ? (
        <Panel
          title="Roles"
          className="workflow-table-panel"
          action={
            <button type="button" className="primary-button" onClick={openCreateRoleDialog}>
              <Plus size={15} aria-hidden />
              New role
            </button>
          }
        >
          <CompactTableShell minWidth="62rem" maxHeight="64vh">
            <CompactTable>
              <colgroup>
                <col style={{ width: "25%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "11%" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "14%" }} />
                <col style={{ width: "16%" }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Kind</th>
                  <th>Tools</th>
                  <th>Approvals</th>
                  <th>Status</th>
                  <th>Updated</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((agent) => (
                  <tr key={agent.id}>
                    <td>
                      <span className="table-primary-cell">
                        <strong>{agent.name}</strong>
                        <em>{agent.mission}</em>
                      </span>
                    </td>
                    <td>
                      <StatusBadge value={isSystemRole(agent.name) ? "system" : "worker"} />
                    </td>
                    <td>{agent.allowed_tools.length}</td>
                    <td>{agent.requires_approval_for.length}</td>
                    <td>
                      <StatusBadge value={agent.enabled ? "enabled" : "disabled"} />
                    </td>
                    <td>
                      <span className="table-truncate">{new Date(agent.updated_at).toLocaleString()}</span>
                    </td>
                    <td>
                      <div className="row-actions">
                        <button type="button" className="secondary-button compact-control" onClick={() => openEditRoleDialog(agent)}>
                          <Pencil size={14} aria-hidden />
                          Edit
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </CompactTable>
          </CompactTableShell>
          {agentsQuery.error ? <div className="error-line">{agentsQuery.error.message}</div> : null}
        </Panel>
      ) : null}
      {activeTab === "execution" ? (
        <Panel title="Latest execution graph" className="latest-execution-panel workflow-execution-panel">
          <AgentGraph run={runQuery.data ?? null} events={events} agents={agents} />
        </Panel>
      ) : null}
      {roleDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="role-title">
          <button type="button" className="modal-backdrop" aria-label="Close dialog" onClick={closeRoleDialog} />
          <section className="modal-panel">
            <header className="modal-header">
              <h2 id="role-title">{roleDialogMode === "edit" ? "Edit role" : "New role"}</h2>
              <button type="button" className="icon-button" aria-label="Close dialog" onClick={closeRoleDialog}>
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitRole}>
              <div className="form-grid">
                <label className="field">
                  <span>Name</span>
                  <input
                    value={roleForm.name}
                    onChange={(event) => setRoleForm({ ...roleForm, name: event.target.value })}
                    disabled={roleDialogMode === "edit"}
                  />
                </label>
                <label className="field">
                  <span>Enabled</span>
                  <select
                    value={roleForm.enabled ? "true" : "false"}
                    onChange={(event) => setRoleForm({ ...roleForm, enabled: event.target.value === "true" })}
                  >
                    <option value="true">enabled</option>
                    <option value="false">disabled</option>
                  </select>
                </label>
              </div>
              <label className="field">
                <span>Mission</span>
                <input value={roleForm.mission} onChange={(event) => setRoleForm({ ...roleForm, mission: event.target.value })} />
              </label>
              <div className="form-grid">
                <label className="field">
                  <span>Allowed tools</span>
                  <input
                    value={roleForm.allowedTools}
                    onChange={(event) => setRoleForm({ ...roleForm, allowedTools: event.target.value })}
                    placeholder={(toolsQuery.data ?? []).slice(0, 2).join(", ") || "native.fs_read, mcp.*"}
                  />
                </label>
                <label className="field">
                  <span>Approval tools</span>
                  <input
                    value={roleForm.approvalTools}
                    onChange={(event) => setRoleForm({ ...roleForm, approvalTools: event.target.value })}
                    placeholder="native.apply_patch"
                  />
                </label>
              </div>
              <label className="field">
                <span>Non goals</span>
                <textarea value={roleForm.nonGoals} rows={4} onChange={(event) => setRoleForm({ ...roleForm, nonGoals: event.target.value })} />
              </label>
              <label className="field">
                <span>Output contract</span>
                <textarea
                  value={roleForm.outputContract}
                  rows={4}
                  onChange={(event) => setRoleForm({ ...roleForm, outputContract: event.target.value })}
                />
              </label>
              {roleMutation.error ? <div className="error-line">{roleMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button type="button" className="secondary-button" onClick={closeRoleDialog} disabled={roleMutation.isPending}>
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={roleMutation.isPending || !roleForm.mission.trim() || (roleDialogMode === "create" && !roleForm.name.trim())}
                >
                  {roleMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  {roleDialogMode === "edit" ? "Save role" : "Create role"}
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
      {graphDialogOpen ? (
        <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="graph-title">
          <button type="button" className="modal-backdrop" aria-label="Close dialog" onClick={closeGraphDialog} />
          <section className="modal-panel graph-wizard-modal">
            <header className="modal-header">
              <h2 id="graph-title">{graphDialogMode === "edit" ? "Edit graph" : graphDialogMode === "clone" ? "Clone graph" : "New graph"}</h2>
              <button type="button" className="icon-button" aria-label="Close dialog" onClick={closeGraphDialog}>
                <X size={16} aria-hidden />
              </button>
            </header>
            <form className="entity-modal-form" onSubmit={submitGraph}>
              <div className="form-grid">
                <label className="field">
                  <span>Name</span>
                  <input value={graphForm.name} onChange={(event) => setGraphForm({ ...graphForm, name: event.target.value })} />
                </label>
                <label className="field">
                  <span>Template</span>
                  <select
                    value={graphForm.templateId}
                    onChange={(event) =>
                      setGraphForm(applyTemplate(graphForm, event.target.value as GraphTemplateId, agents))
                    }
                  >
                    {Object.entries(GRAPH_TEMPLATES).map(([id, template]) => (
                      <option key={id} value={id}>
                        {template.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="form-grid">
                <label className="field">
                  <span>Default profile</span>
                  <select
                    value={graphForm.defaultProfileId}
                    onChange={(event) => setGraphForm({ ...graphForm, defaultProfileId: event.target.value })}
                  >
                    <option value="">none/default</option>
                    {profiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Default graph</span>
                  <select
                    value={graphForm.isDefault ? "true" : "false"}
                    onChange={(event) => setGraphForm({ ...graphForm, isDefault: event.target.value === "true" })}
                  >
                    <option value="false">no</option>
                    <option value="true">yes</option>
                  </select>
                </label>
              </div>
              <label className="field">
                <span>Workers</span>
                <select multiple value={graphForm.workerOrder} onChange={(event) => setGraphForm(updateWorkers(graphForm, event.currentTarget, agents))}>
                  {agents
                    .filter((agent) => agent.enabled && !isSystemRole(agent.name))
                    .map((agent) => (
                      <option key={agent.id} value={agent.id}>
                        {agent.name}
                      </option>
                    ))}
                </select>
              </label>
              <div className="graph-binding-grid">
                {graphForm.roleIds.map((roleId) => (
                  <label className="field" key={roleId}>
                    <span>{roleNameById.get(roleId) ?? roleId.slice(0, 8)}</span>
                    <select
                      value={graphForm.roleProfileIds[roleId] ?? ""}
                      onChange={(event) =>
                        setGraphForm({
                          ...graphForm,
                          roleProfileIds: {
                            ...graphForm.roleProfileIds,
                            [roleId]: event.target.value,
                          },
                        })
                      }
                    >
                      <option value="">graph default</option>
                      {profiles.map((profile) => (
                        <option key={profile.id} value={profile.id}>
                          {profile.name}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <div className="form-grid">
                <label className="field">
                  <span>Enabled</span>
                  <select
                    value={graphForm.enabled ? "true" : "false"}
                    onChange={(event) => setGraphForm({ ...graphForm, enabled: event.target.value === "true" })}
                  >
                    <option value="true">enabled</option>
                    <option value="false">disabled</option>
                  </select>
                </label>
                <div className="graph-preview">
                  {graphEdges.map((edge) => (
                    <span key={`${edge.from_role}-${edge.to_role}`}>
                      {`${roleNameById.get(edge.from_role) ?? edge.from_role.slice(0, 8)} -> ${
                        roleNameById.get(edge.to_role) ?? edge.to_role.slice(0, 8)
                      }`}
                    </span>
                  ))}
                </div>
              </div>
              {!graphValidation.ok ? <div className="error-line">{graphValidation.error}</div> : null}
              {graphMutation.error ? <div className="error-line">{graphMutation.error.message}</div> : null}
              <footer className="modal-actions">
                <button type="button" className="secondary-button" onClick={closeGraphDialog} disabled={graphMutation.isPending}>
                  Cancel
                </button>
                <button className="primary-button" type="submit" disabled={graphMutation.isPending || !graphValidation.ok}>
                  {graphMutation.isPending ? (
                    <RefreshCw size={15} aria-hidden className="spin" />
                  ) : (
                    <Plus size={15} aria-hidden />
                  )}
                  {graphDialogMode === "edit" ? "Save graph" : "Create graph"}
                </button>
              </footer>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function applyTemplate(form: GraphFormState, templateId: GraphTemplateId, agents: AgentSpec[]): GraphFormState {
  const byName = new Map(agents.map((agent) => [agent.name, agent]));
  const supervisor = byName.get("supervisor");
  const reviewer = byName.get("reviewer");
  const workers = GRAPH_TEMPLATES[templateId].workers
    .map((name) => byName.get(name))
    .filter((agent): agent is AgentSpec => Boolean(agent?.enabled));
  const workerOrder = templateId === "blank" ? [] : workers.map((agent) => agent.id);
  const roleIds = [supervisor?.id, ...workerOrder, reviewer?.id].filter((value): value is string => Boolean(value));
  return {
    ...form,
    templateId,
    roleIds,
    workerOrder,
    roleProfileIds: retainBindings(form.roleProfileIds, roleIds),
  };
}

function graphToForm(graph: AgentGraphConfig, agents: AgentSpec[], mode: "edit" | "clone"): GraphFormState {
  const roleById = new Map(agents.map((agent) => [agent.id, agent]));
  const workerOrder = graph.role_ids.filter((roleId) => {
    const role = roleById.get(roleId);
    return role && !isSystemRole(role.name);
  });
  return {
    id: mode === "edit" ? graph.id : null,
    name: mode === "clone" ? `Copy of ${graph.name}` : graph.name,
    templateId: "blank",
    roleIds: graph.role_ids,
    workerOrder,
    defaultProfileId: graph.default_model_profile_id ?? "",
    roleProfileIds: graph.role_model_profile_ids,
    isDefault: mode === "edit" ? graph.is_default : false,
    enabled: graph.enabled,
  };
}

function updateWorkers(form: GraphFormState, select: HTMLSelectElement, agents: AgentSpec[]): GraphFormState {
  const byName = new Map(agents.map((agent) => [agent.name, agent]));
  const supervisor = byName.get("supervisor");
  const reviewer = byName.get("reviewer");
  const workerOrder = Array.from(select.selectedOptions).map((option) => option.value);
  const roleIds = [supervisor?.id, ...workerOrder, reviewer?.id].filter((value): value is string => Boolean(value));
  return {
    ...form,
    templateId: "blank",
    roleIds,
    workerOrder,
    roleProfileIds: retainBindings(form.roleProfileIds, roleIds),
  };
}

function buildGraphEdges(form: GraphFormState, agents: AgentSpec[]) {
  const byName = new Map(agents.map((agent) => [agent.name, agent]));
  const supervisor = byName.get("supervisor");
  const reviewer = byName.get("reviewer");
  if (!supervisor || !reviewer || !form.workerOrder.length) {
    return [];
  }
  const roleIds = [supervisor.id, ...form.workerOrder, reviewer.id];
  const edges = [];
  for (let index = 0; index < roleIds.length - 1; index += 1) {
    edges.push({ from_role: roleIds[index], to_role: roleIds[index + 1] });
  }
  return edges;
}

function validateGraphForm(form: GraphFormState, agents: AgentSpec[]): { ok: true } | { ok: false; error: string } {
  if (!form.name.trim()) {
    return { ok: false, error: "Graph name is required" };
  }
  const names = new Set(agents.filter((agent) => form.roleIds.includes(agent.id)).map((agent) => agent.name));
  if (!names.has("supervisor") || !names.has("reviewer")) {
    return { ok: false, error: "Graph requires supervisor and reviewer roles" };
  }
  if (!form.workerOrder.length) {
    return { ok: false, error: "Select at least one worker role" };
  }
  return { ok: true };
}

function compactBindings(bindings: Record<string, string>) {
  return Object.fromEntries(Object.entries(bindings).filter(([, profileId]) => Boolean(profileId)));
}

function retainBindings(bindings: Record<string, string>, roleIds: string[]) {
  const allowed = new Set(roleIds);
  return Object.fromEntries(Object.entries(bindings).filter(([roleId]) => allowed.has(roleId)));
}

function splitComma(value: string) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function splitLines(value: string) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function isSystemRole(roleName: string) {
  return roleName === "supervisor" || roleName === "reviewer";
}
