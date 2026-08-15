import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamPost } from "../api";
import type { Invocation, Message, StreamEvent } from "../types";

export interface LiveState {
  assistantText: string;
  invocations: Invocation[];
  streaming: boolean;
}

const EMPTY_LIVE: LiveState = { assistantText: "", invocations: [], streaming: false };

export function useChatStream(conversationId: string | null, onTitle: (t: string) => void) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [invocations, setInvocations] = useState<Invocation[]>([]);
  const [totalTokens, setTotalTokens] = useState(0);
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [pending, setPending] = useState<Invocation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (!conversationId) return;
    const detail = await api.getConversation(conversationId);
    setMessages(detail.messages);
    setInvocations(detail.invocations);
    setTotalTokens(detail.total_tokens);
    setPending(detail.invocations.filter((i) => i.status === "pending_approval"));
  }, [conversationId]);

  useEffect(() => {
    setLive(EMPTY_LIVE);
    setPending([]);
    setError(null);
    setWarning(null);
    if (conversationId) void refresh();
    else {
      setMessages([]);
      setInvocations([]);
    }
  }, [conversationId, refresh]);

  const handle = useCallback(
    (event: StreamEvent) => {
      switch (event.type) {
        case "message_start":
          setLive((l) => ({ ...l, streaming: true }));
          break;
        case "token":
          setLive((l) => ({ ...l, assistantText: l.assistantText + event.text }));
          break;
        case "tool_call":
          setLive((l) => ({ ...l, invocations: [...l.invocations, event.invocation] }));
          break;
        case "tool_result":
          setLive((l) => ({
            ...l,
            invocations: l.invocations.map((i) =>
              i.id === event.invocation.id ? event.invocation : i,
            ),
          }));
          break;
        case "approval_required":
          setPending(event.invocations);
          break;
        case "title":
          onTitle(event.title);
          break;
        case "warning":
          // e.g. the model hit its output token limit: the answer is genuinely
          // incomplete, which is worth distinguishing from a transport fault.
          setWarning(event.message);
          break;
        case "error":
          setError(event.message);
          break;
        default:
          break;
      }
    },
    [onTitle],
  );

  const drive = useCallback(
    async (url: string, body: unknown, optimistic?: Message) => {
      if (!conversationId) return;
      setError(null);
      setWarning(null);
      setBusy(true);
      setLive({ ...EMPTY_LIVE, streaming: true });
      if (optimistic) setMessages((m) => [...m, optimistic]);

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await streamPost(url, body, handle, controller.signal);
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError(String(err));
      } finally {
        abortRef.current = null;
        setBusy(false);
        // The server persisted everything as it went, so the canonical
        // transcript is one GET away — cheaper than reconciling by hand.
        await refresh();
        setLive(EMPTY_LIVE);
      }
    },
    [conversationId, handle, refresh],
  );

  const send = useCallback(
    (text: string) =>
      drive(`/api/conversations/${conversationId}/messages`, { message: text }, {
        id: `optimistic-${Date.now()}`,
        role: "user",
        content: text,
        tool_calls: null,
        tool_call_id: null,
        status: "complete",
        seq: Number.MAX_SAFE_INTEGER,
        created_at: new Date().toISOString(),
        token_usage: null,
        model: null,
      }),
    [conversationId, drive],
  );

  const decide = useCallback(
    (decisions: { invocation_id: string; decision: "approve" | "deny" }[]) => {
      setPending([]);
      return drive(`/api/conversations/${conversationId}/approvals`, { decisions });
    },
    [conversationId, drive],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return {
    messages,
    invocations,
    totalTokens,
    live,
    pending,
    busy,
    error,
    warning,
    send,
    decide,
    stop,
    refresh,
  };
}
