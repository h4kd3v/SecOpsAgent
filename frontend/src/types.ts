export interface Session {
  id: string;
  label: string;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** Running total for the thread, stored server-side rather than summed. */
  total_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  /** True if any turn's usage was approximated because the gateway omitted it. */
  usage_estimated?: boolean;
  /** Who started the thread — shown when it was not you. */
  author_session_id?: string | null;
  author_label?: string | null;
  /** Money, at the rates in force when each turn ran. Serialised as a string. */
  cost_usd?: string;
  pinned?: boolean;
  tags?: string[];
}

export interface Invocation {
  id: string;
  tool_call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  status:
    | "pending_approval"
    | "denied"
    | "running"
    | "succeeded"
    | "failed"
    | "timeout"
    | "cancelled";
  is_write: boolean;
  latency_ms: number | null;
  error: string | null;
  /** First slice of what the tool returned. The rest is fetched on demand. */
  result_preview?: string | null;
  /** Total characters the tool returned, so the UI knows if there is more. */
  result_chars?: number;
}

export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  /** true when the gateway omitted usage and the backend approximated it */
  estimated?: boolean;
}

export interface Tool {
  name: string;
  description: string;
  read_only: boolean;
}

export interface ToolCatalog {
  tools: Tool[];
  fetched_at: string | null;
  age_seconds: number | null;
  /** "cache" from Postgres, "live" just fetched, "stale" past TTL but MCP unreachable */
  source: "cache" | "live" | "stale";
  stale: boolean;
  error: string | null;
  ttl_hours: number;
}

export interface AppConfig {
  /** The id currently configured, for spotting answers from an earlier model. */
  model_name: string;
  model_display_name: string;
  require_approval_for_write: boolean;
  demo_mode: boolean;
}

export interface Message {
  id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls: unknown[] | null;
  tool_call_id: string | null;
  status: string;
  /** The model's working on the way to this answer, when the gateway streams it. */
  reasoning: string | null;
  /** Who wrote this, on a shared thread. Null for assistant and tool rows. */
  author_label?: string | null;
  /** Why this turn failed, if it did. Persisted, so it survives a reload. */
  error: string | null;
  seq: number;
  created_at: string;
  token_usage: TokenUsage | null;
  /** What this turn cost, or null when the model has no configured rate. */
  cost_usd?: string | null;
  model: string | null;
  /** How the shift rated this answer, and whether you were part of it. */
  feedback_up?: number;
  feedback_down?: number;
  my_feedback?: "up" | "down" | null;
}

/** Mirrors the `type` field of every SSE frame the agent loop emits. */
export type StreamEvent =
  | { type: "user_message"; id: string; seq: number }
  | { type: "message_start"; id: string; seq: number }
  | { type: "token"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "tool_call_delta"; index: number; name: string; arguments: string }
  | { type: "tool_call"; invocation: Invocation; awaiting_approval: boolean }
  | { type: "tool_result"; invocation: Invocation }
  | { type: "approval_required"; invocations: Invocation[] }
  | { type: "title"; title: string }
  | { type: "done"; id: string; model?: string | null; usage?: TokenUsage | null }
  | { type: "warning"; message: string }
  | { type: "error"; message: string }
  | { type: "stream_end" };

/** Frames on the long-lived /api/events connection that drives the sidebar. */
export type SidebarEvent =
  | { type: "resync" }
  | { type: "conversation_created"; conversation: Conversation }
  | { type: "conversation_updated"; conversation: Conversation }
  | { type: "conversation_archived"; conversation: Conversation };
