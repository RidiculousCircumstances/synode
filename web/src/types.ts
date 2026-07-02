export type RunStatus =
  | "created"
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_operator"
  | "cancelling"
  | "completed"
  | "failed"
  | "failed_verification"
  | "cancelled";

export type RunMode = "general" | "coding";
export type InteractionMode = "auto" | "plan_review" | "plan_only";

export type RuntimeBackend = string;
export type NativeLoopMode = "strict" | "guided" | "autonomous";
export type AgentGraphNodeKind = "control" | "worker";
export type MCPServerTransport = "stdio" | "sse" | "streamable_http";

export type ModelProviderType = "fake" | "ollama" | "openai_compatible";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type ThreadStatus = "active" | "archived";

export type ThreadMessageAuthorType = "user" | "agent" | "system";

export type ThreadMessageType =
  | "text"
  | "run_summary"
  | "run_report"
  | "approval_request"
  | "approval_decision"
  | "operator_request"
  | "operator_decision"
  | "final";

export interface Run {
  id: string;
  thread_id: string;
  status: RunStatus;
  mode: RunMode;
  interaction_mode: InteractionMode;
  task: string;
  workspace: string | null;
  model_provider: string;
  default_model_profile_id: string | null;
  role_model_profile_ids: Record<string, string>;
  agent_graph_id: string | null;
  agent_graph_snapshot: Record<string, unknown>;
  observability_trace_id: string | null;
  final_answer: string | null;
  error: string | null;
  worker_id: string | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Thread {
  id: string;
  title: string;
  status: ThreadStatus;
  latest_run_id: string | null;
  latest_run_status: RunStatus | null;
  last_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface ThreadMessage {
  id: number;
  thread_id: string;
  run_id: string | null;
  author_type: ThreadMessageAuthorType;
  author_name: string;
  message_type: ThreadMessageType;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ThreadDetail {
  thread: Thread;
  runs: Run[];
  messages: ThreadMessage[];
}

export interface RunEvent {
  id: number;
  run_id: string;
  event_type: string;
  role: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Artifact {
  id: string;
  run_id: string;
  kind: string;
  path: string | null;
  content: Record<string, unknown>;
  created_at: string;
}

export interface PlanReportStep {
  role: string;
  task: string;
  status: "planned" | "running" | "completed" | "blocked";
  tool_count: number;
}

export interface RoleOutputReport {
  role: string;
  summary: string;
  tool_count: number;
  failed_tool_count: number;
  risks: string[];
}

export interface PatchFileReport {
  path: string;
  operation: string;
  status: "ok" | "failed" | "pending_approval" | "skipped";
  summary: string | null;
  error: string | null;
}

export interface PatchResultsReport {
  status: "not_applicable" | "ok" | "failed" | "pending_approval" | "no_change";
  files: PatchFileReport[];
  raw_count: number;
}

export interface VerificationCommandReport {
  command: string;
  status: "passed" | "failed" | "skipped" | "unknown";
  summary: string | null;
}

export interface VerificationReport {
  status: "not_run" | "passed" | "failed" | "skipped";
  commands: VerificationCommandReport[];
  reason: string | null;
}

export interface ToolActivityReport {
  role: string | null;
  tool_name: string;
  status: string;
  risk: string | null;
  title: string;
  target: string | null;
  approval_id: string | null;
}

export interface RunReport {
  version: number;
  run_id: string;
  thread_id: string;
  mode: string;
  status: string;
  headline: string;
  summary: string;
  plan: PlanReportStep[];
  role_outputs: RoleOutputReport[];
  patch_results: PatchResultsReport;
  verification: VerificationReport;
  tool_activity: ToolActivityReport[];
  blockers: string[];
  advisory: string[];
  diagnostics: Record<string, unknown>;
  raw_refs: Record<string, string>;
  artifact_id?: string | null;
  created_at?: string | null;
}

export interface Approval {
  id: string;
  run_id: string;
  tool_name: string;
  action: string;
  reason: string;
  payload: Record<string, unknown>;
  status: ApprovalStatus;
  decision_reason: string | null;
  created_at: string;
  decided_at: string | null;
}

export type OperatorRequestKind = "plan_review" | "ambiguity" | "state_edit";
export type OperatorRequestStatus = "pending" | "resolved" | "cancelled";
export type OperatorResponseType = "approve" | "edit" | "reject" | "respond";

export interface OperatorRequest {
  id: string;
  run_id: string;
  thread_id: string;
  node_id: string | null;
  role: string | null;
  kind: OperatorRequestKind;
  prompt: string;
  context: Record<string, unknown>;
  proposed_payload: Record<string, unknown>;
  status: OperatorRequestStatus;
  response_payload: Record<string, unknown>;
  created_at: string;
  resolved_at: string | null;
  cancelled_at: string | null;
  consumed_at: string | null;
}

export interface ToolAudit {
  id: number;
  run_id: string;
  role: string;
  tool_name: string;
  risk: string;
  status: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  approval_id: string | null;
  created_at: string;
}

export interface TokenUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

export interface RunMetrics {
  run_id: string;
  status: RunStatus;
  duration_ms: number | null;
  event_count: number;
  model_call_count: number;
  tool_call_count: number;
  approval_count: number;
  pending_approval_count: number;
  failed_tool_call_count: number;
  token_usage: TokenUsage;
  provider_usage: Record<string, TokenUsage>;
  latency_ms_by_role: Record<string, number>;
}

export interface SystemMetrics {
  process: {
    pid: number;
    uptime_seconds: number;
    cpu_percent: number;
    memory_rss_bytes: number;
    memory_percent: number;
  };
  gpu: Array<{
    available: boolean;
    name: string | null;
    utilization_percent: number | null;
    memory_used_mb: number | null;
    memory_total_mb: number | null;
    error: string | null;
  }>;
}

export interface SandboxStatus {
  backend: string;
  available: boolean;
  detail: string | null;
  cpu_seconds: number;
  memory_mb: number;
  disk_mb: number;
  output_max_bytes: number;
}

export interface WorkerHeartbeat {
  worker_id: string;
  hostname: string;
  pid: number;
  status: string;
  current_run_id: string | null;
  started_at: string;
  heartbeat_at: string;
}

export interface QueueStatus {
  backend: string;
  available: boolean;
  detail: string | null;
  queue_name: string | null;
  pending_jobs: number | null;
  running_jobs: number | null;
  failed_jobs: number | null;
}

export interface ExecutionBackendStatus {
  backend: string;
  available: boolean;
  detail: string | null;
}

export interface RuntimeStatus {
  queue_depth: number;
  running_count: number;
  cancelling_count: number;
  stale_running_count: number;
  worker_concurrency: number;
  secrets_configured: boolean;
  queue: QueueStatus;
  execution_backends: Record<string, ExecutionBackendStatus>;
  workers: WorkerHeartbeat[];
  sandbox: SandboxStatus;
}

export interface AgentSpec {
  id: string;
  name: string;
  mission: string;
  non_goals: string[];
  allowed_tools: string[];
  requires_approval_for: string[];
  output_contract: string;
  builtin: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelHealth {
  provider: string;
  ok: boolean;
  profile_id?: string | null;
  profile_name?: string | null;
  provider_type?: ModelProviderType | string | null;
  model?: string | null;
  error?: string | null;
}

export interface Secret {
  id: string;
  name: string;
  secret_set: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelProfile {
  id: string;
  name: string;
  provider_type: ModelProviderType;
  base_url: string | null;
  model: string;
  options: Record<string, unknown>;
  secret_id: string | null;
  secret_set: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelProfileTestCheck {
  name: "health" | "structured_output" | "streaming";
  ok: boolean;
  supported: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface ModelProfileTestResult {
  profile_id: string;
  ok: boolean;
  provider_type: ModelProviderType;
  model: string;
  capabilities: {
    streaming: boolean;
    structured_output: boolean;
  };
  checks: ModelProfileTestCheck[];
}

export interface MCPServer {
  id: string;
  name: string;
  transport: MCPServerTransport;
  config: Record<string, unknown>;
  enabled: boolean;
  tools: string[];
  last_error: string | null;
  last_discovered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentGraphNode {
  id: string;
  role_id: string;
  label: string;
  kind: AgentGraphNodeKind;
}

export interface AgentGraphNodeEdge {
  from_node: string;
  to_node: string;
}

export interface AgentGraph {
  id: string;
  name: string;
  graph_schema_version: number;
  nodes: AgentGraphNode[];
  node_edges: AgentGraphNodeEdge[];
  default_model_profile_id: string | null;
  role_model_profile_ids: Record<string, string>;
  node_runtime_bindings: Record<string, RuntimeBackend>;
  node_contracts: Record<string, string>;
  node_loop_policies: Record<string, NativeLoopMode>;
  is_default: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

declare global {
  interface Window {
    SYNODE_UI_CONFIG?: {
      apiBaseUrl?: string;
      apiPort?: string;
    };
  }
}
