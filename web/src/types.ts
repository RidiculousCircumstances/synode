export type RunStatus =
  | "created"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "failed_verification";

export type RunMode = "general" | "coding";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export interface Run {
  id: string;
  status: RunStatus;
  mode: RunMode;
  task: string;
  workspace: string | null;
  model_provider: string;
  observability_trace_id: string | null;
  final_answer: string | null;
  created_at: string;
  updated_at: string;
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
  name: string;
  mission: string;
  allowed_tools: string[];
}

export interface ModelHealth {
  provider: string;
  ok: boolean;
  model?: string | null;
  error?: string | null;
}

declare global {
  interface Window {
    SYNODE_UI_CONFIG?: {
      apiBaseUrl?: string;
      apiPort?: string;
    };
  }
}
