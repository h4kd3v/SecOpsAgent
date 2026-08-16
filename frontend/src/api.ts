import type {
  AppConfig,
  Conversation,
  Invocation,
  Message,
  Session,
  StreamEvent,
  ToolCatalog,
} from "./types";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  /** Get-or-create the anonymous session. No login, no registration —
   *  the signed cookie this sets is the only identity in the system. */
  openSession: () => request<Session>("/api/session", { method: "POST" }),

  config: () => request<AppConfig>("/api/config"),
  listTools: () => request<ToolCatalog>("/api/tools"),
  refreshTools: () =>
    request<ToolCatalog>("/api/tools/refresh", { method: "POST" }),

  listConversations: () => request<Conversation[]>("/api/conversations"),
  createConversation: () =>
    request<Conversation>("/api/conversations", { method: "POST" }),
  getConversation: (id: string) =>
    request<{
      conversation: Conversation;
      messages: Message[];
      invocations: Invocation[];
      total_tokens: number;
    }>(`/api/conversations/${id}`),
  /** The full text of one tool result, deliberately not carried by the
   *  transcript — a single UDM page runs to hundreds of kilobytes. */
  invocationResult: (conversationId: string, invocationId: string) =>
    request<{ id: string; text: string }>(
      `/api/conversations/${conversationId}/invocations/${invocationId}/result`,
    ),

  updateConversation: (
    id: string,
    changes: { title?: string; pinned?: boolean; tags?: string[] },
  ) =>
    request<Conversation>(`/api/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),

  rateMessage: (
    conversationId: string,
    messageId: string,
    rating: "up" | "down" | null,
  ) =>
    request<{ up: number; down: number; mine: "up" | "down" | null }>(
      `/api/conversations/${conversationId}/messages/${messageId}/feedback`,
      rating === null
        ? { method: "DELETE" }
        : { method: "PUT", body: JSON.stringify({ rating }) },
    ),

  archiveConversation: (id: string) =>
    request<void>(`/api/conversations/${id}`, { method: "DELETE" }),
};

/**
 * POST a body and consume the SSE response.
 *
 * EventSource can't do POST or send cookies cross-origin reliably, so we read
 * the response stream by hand. Frames are `event: <name>\ndata: <json>\n\n`.
 */
export async function streamPost(
  url: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    onEvent({ type: "error", message: detail.detail ?? `HTTP ${response.status}` });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split: number;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const dataLine = frame
        .split("\n")
        .find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      try {
        onEvent(JSON.parse(dataLine.slice(5).trim()) as StreamEvent);
      } catch {
        // A truncated frame means the connection died mid-write; ignore it.
      }
    }
  }
}
