import type {
  AgentGraph,
  AgentSpec,
  Approval,
  Artifact,
  ModelHealth,
  ModelProfile,
  ModelProfileTestResult,
  MCPServer,
  MCPServerTransport,
  Run,
  RunEvent,
  RunMetrics,
  RunMode,
  RuntimeStatus,
  SandboxStatus,
  Secret,
  Thread,
  ThreadDetail,
  ThreadMessage,
  ThreadStatus,
  SystemMetrics,
  ToolAudit,
} from "@/types";

export type UiConfig = {
  apiBaseUrl: string;
  apiPort: string;
};

let cachedApiBaseUrl: string | null = null;

export async function getApiBaseUrl(): Promise<string> {
  if (cachedApiBaseUrl) {
    return cachedApiBaseUrl;
  }

  const config = await readUiConfig();
  cachedApiBaseUrl = resolveApiBaseUrl(config);
  return cachedApiBaseUrl;
}

export function clearApiBaseUrlCache() {
  cachedApiBaseUrl = null;
}

async function readUiConfig(): Promise<UiConfig> {
  if (typeof window === "undefined") {
    return {
      apiBaseUrl: process.env.SYNODE_UI_API_BASE_URL ?? "auto",
      apiPort: process.env.SYNODE_UI_API_PORT ?? "8787",
    };
  }

  const injected = window.SYNODE_UI_CONFIG;
  if (injected) {
    return {
      apiBaseUrl: injected.apiBaseUrl ?? "auto",
      apiPort: injected.apiPort ?? "8787",
    };
  }

  const response = await fetch("/api/ui-config", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Cannot load Synode UI config: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as UiConfig;
}

function resolveApiBaseUrl(config: UiConfig): string {
  const configuredBaseUrl = config.apiBaseUrl.trim();
  if (configuredBaseUrl && configuredBaseUrl !== "auto") {
    const parsed = new URL(configuredBaseUrl);
    return parsed.toString().replace(/\/+$/, "");
  }

  const port = normalizePort(config.apiPort);
  if (typeof window === "undefined") {
    throw new Error("SYNODE_UI_API_BASE_URL=auto requires browser host resolution");
  }

  const protocol = window.location.protocol === "https:" ? "https:" : "http:";
  const host = window.location.hostname;
  if (!host) {
    throw new Error("Cannot resolve Synode API host from current browser location");
  }
  return `${protocol}//${host}:${port}`;
}

function normalizePort(value: string): string {
  const port = value.trim() || "8787";
  if (!/^\d+$/.test(port)) {
    throw new Error(`Invalid Synode API port: ${value}`);
  }
  return port;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const apiBaseUrl = await getApiBaseUrl();
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function listRuns(): Promise<Run[]> {
  return request<Run[]>("/runs");
}

export function listThreads(status: ThreadStatus | "all" = "active", search?: string): Promise<Thread[]> {
  const params = new URLSearchParams();
  if (status !== "all") {
    params.set("status", status);
  }
  if (search?.trim()) {
    params.set("search", search.trim());
  }
  const query = params.toString();
  return request<Thread[]>(`/threads${query ? `?${query}` : ""}`);
}

export function getThread(threadId: string): Promise<ThreadDetail> {
  return request<ThreadDetail>(`/threads/${threadId}`);
}

export function createThread(payload: {
  message: string;
  title?: string | null;
  workspace?: string | null;
  model_provider?: string | null;
  default_model_profile_id?: string | null;
  role_model_profile_ids?: Record<string, string>;
  agent_graph_id?: string | null;
  mode: RunMode;
}): Promise<ThreadDetail> {
  return request<ThreadDetail>("/threads", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateThread(threadId: string, payload: { title: string }): Promise<Thread> {
  return request<Thread>(`/threads/${threadId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function archiveThread(threadId: string): Promise<Thread> {
  return request<Thread>(`/threads/${threadId}/archive`, { method: "POST", body: "{}" });
}

export function listThreadMessages(threadId: string): Promise<ThreadMessage[]> {
  return request<ThreadMessage[]>(`/threads/${threadId}/messages`);
}

export function createThreadRun(
  threadId: string,
  payload: {
    message: string;
    workspace?: string | null;
    model_provider?: string | null;
    default_model_profile_id?: string | null;
    role_model_profile_ids?: Record<string, string>;
    agent_graph_id?: string | null;
    mode: RunMode;
  },
): Promise<Run> {
  return request<Run>(`/threads/${threadId}/runs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}`);
}

export function createRun(payload: {
  task: string;
  workspace?: string | null;
  model_provider?: string | null;
  default_model_profile_id?: string | null;
  role_model_profile_ids?: Record<string, string>;
  agent_graph_id?: string | null;
  mode: RunMode;
}): Promise<Run> {
  return request<Run>("/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function stopRun(runId: string, reason = "Stopped from UI"): Promise<Run> {
  return request<Run>(`/runs/${runId}/stop`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function resumeRun(runId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/runs/${runId}/resume`, {
    method: "POST",
    body: "{}",
  });
}

export function listEvents(runId: string, afterId = 0): Promise<RunEvent[]> {
  return request<RunEvent[]>(`/runs/${runId}/events?after_id=${afterId}`);
}

export function listArtifacts(runId: string): Promise<Artifact[]> {
  return request<Artifact[]>(`/runs/${runId}/artifacts`);
}

export function listToolAudit(runId: string): Promise<ToolAudit[]> {
  return request<ToolAudit[]>(`/runs/${runId}/tool-audit`);
}

export function listRunApprovals(runId: string): Promise<Approval[]> {
  return request<Approval[]>(`/runs/${runId}/approvals`);
}

export function decideApproval(
  approvalId: string,
  decision: "approve" | "reject",
  reason: string,
): Promise<{ status: string }> {
  return request<{ status: string }>(`/approvals/${approvalId}/${decision}`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function getRunMetrics(runId: string): Promise<RunMetrics> {
  return request<RunMetrics>(`/runs/${runId}/metrics`);
}

export function getSystemMetrics(): Promise<SystemMetrics> {
  return request<SystemMetrics>("/metrics/system");
}

export function getRuntimeStatus(): Promise<RuntimeStatus> {
  return request<RuntimeStatus>("/runtime/status");
}

export function getSandboxStatus(): Promise<SandboxStatus> {
  return request<SandboxStatus>("/runtime/sandbox");
}

export function listAgents(): Promise<AgentSpec[]> {
  return request<AgentSpec[]>("/agents");
}

export function createAgent(payload: {
  name: string;
  mission: string;
  non_goals?: string[];
  allowed_tools?: string[];
  requires_approval_for?: string[];
  output_contract?: string;
  enabled?: boolean;
}): Promise<AgentSpec> {
  return request<AgentSpec>("/agents", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAgent(
  roleId: string,
  payload: {
    mission?: string;
    non_goals?: string[];
    allowed_tools?: string[];
    requires_approval_for?: string[];
    output_contract?: string;
    enabled?: boolean;
  },
): Promise<AgentSpec> {
  return request<AgentSpec>(`/agents/${roleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function listAgentGraphs(): Promise<AgentGraph[]> {
  return request<AgentGraph[]>("/agent-graphs");
}

export function createAgentGraph(payload: {
  name: string;
  graph_schema_version?: number;
  nodes?: Array<{ id: string; role_id: string; label: string; kind: "control" | "worker" }>;
  node_edges?: Array<{ from_node: string; to_node: string }>;
  default_model_profile_id?: string | null;
  role_model_profile_ids?: Record<string, string>;
  node_runtime_bindings?: Record<string, string>;
  node_contracts?: Record<string, string>;
  is_default?: boolean;
  enabled?: boolean;
}): Promise<AgentGraph> {
  return request<AgentGraph>("/agent-graphs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAgentGraph(
  graphId: string,
  payload: {
    name?: string;
    graph_schema_version?: number;
    nodes?: Array<{ id: string; role_id: string; label: string; kind: "control" | "worker" }>;
    node_edges?: Array<{ from_node: string; to_node: string }>;
    default_model_profile_id?: string | null;
    role_model_profile_ids?: Record<string, string>;
    node_runtime_bindings?: Record<string, string>;
    node_contracts?: Record<string, string>;
    is_default?: boolean;
    enabled?: boolean;
  },
): Promise<AgentGraph> {
  return request<AgentGraph>(`/agent-graphs/${graphId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function listSecrets(): Promise<Secret[]> {
  return request<Secret[]>("/secrets");
}

export function createSecret(payload: { name: string; value: string }): Promise<Secret> {
  return request<Secret>("/secrets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listModelProfiles(): Promise<ModelProfile[]> {
  return request<ModelProfile[]>("/model-profiles");
}

export function createModelProfile(payload: {
  name: string;
  provider_type: string;
  base_url?: string | null;
  model: string;
  options?: Record<string, unknown>;
  secret_id?: string | null;
  enabled?: boolean;
}): Promise<ModelProfile> {
  return request<ModelProfile>("/model-profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateModelProfile(
  profileId: string,
  payload: {
    name?: string;
    provider_type?: string;
    base_url?: string | null;
    model?: string;
    options?: Record<string, unknown>;
    secret_id?: string | null;
    enabled?: boolean;
  },
): Promise<ModelProfile> {
  return request<ModelProfile>(`/model-profiles/${profileId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function testModelProfile(profileId: string): Promise<ModelProfileTestResult> {
  return request<ModelProfileTestResult>(`/model-profiles/${profileId}/test`, {
    method: "POST",
    body: "{}",
  });
}

export function listMcpServers(): Promise<MCPServer[]> {
  return request<MCPServer[]>("/mcp/servers");
}

export function createMcpServer(payload: {
  name: string;
  transport: MCPServerTransport;
  config: Record<string, unknown>;
  enabled?: boolean;
}): Promise<MCPServer> {
  return request<MCPServer>("/mcp/servers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateMcpServer(
  serverId: string,
  payload: {
    name?: string;
    transport?: MCPServerTransport;
    config?: Record<string, unknown>;
    enabled?: boolean;
  },
): Promise<MCPServer> {
  return request<MCPServer>(`/mcp/servers/${serverId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteMcpServer(serverId: string): Promise<void> {
  return request<void>(`/mcp/servers/${serverId}`, { method: "DELETE" });
}

export function discoverMcpServer(serverId: string): Promise<MCPServer> {
  return request<MCPServer>(`/mcp/servers/${serverId}/discover`, {
    method: "POST",
    body: "{}",
  });
}

export function getModelHealth(): Promise<ModelHealth[]> {
  return request<ModelHealth[]>("/models/health");
}

export function listTools(): Promise<string[]> {
  return request<{ tools: string[] }>("/tools").then((response) => response.tools);
}

export async function eventStreamUrl(runId: string, afterId: number): Promise<string> {
  const apiBaseUrl = await getApiBaseUrl();
  return `${apiBaseUrl}/runs/${runId}/events/stream?after_id=${afterId}`;
}
