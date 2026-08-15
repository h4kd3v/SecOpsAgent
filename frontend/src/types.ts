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
    | "timeout";
  is_write: boolean;
  latency_ms: number | null;
  error: string | null;
  result?: { text?: string } | null;
  result_preview?: string | null;
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
  seq: number;
  created_at: string;
  token_usage: TokenUsage | null;
  model: string | null;
}

/** Mirrors the `type` field of every SSE frame the agent loop emits. */
export type StreamEvent =
  | { type: "user_message"; id: string; seq: number }
  | { type: "message_start"; id: string; seq: number }
  | { type: "token"; text: string }
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
