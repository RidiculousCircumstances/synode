export type RunStatus =
  | "created"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "failed_verification"
  | "cancelled";

export type RunMode = "general" | "coding";

export type ModelProviderType = "fake" | "ollama" | "openai_compatible";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type ThreadStatus = "active" | "archived";

export type ThreadMessageAuthorType = "user" | "agent" | "system";

export type ThreadMessageType =
  | "text"
  | "run_summary"
  | "approval_request"
  | "approval_decision"
  | "final";

export interface Run {
  id: string;
  thread_id: string;
  status: RunStatus;
  mode: RunMode;
  task: string;
  workspace: string | null;
  model_provider: string;
  default_model_profile_id: string | null;
  role_model_profile_ids: Record<string, string>;
  agent_graph_id: string | null;
  agent_graph_snapshot: Record<string, unknown>;
  observability_trace_id: string | null;
  final_answer: string | null;
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

export interface AgentGraphEdge {
  from_role: string;
  to_role: string;
}

export interface AgentGraph {
  id: string;
  name: string;
  role_ids: string[];
  edges: AgentGraphEdge[];
  default_model_profile_id: string | null;
  role_model_profile_ids: Record<string, string>;
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
