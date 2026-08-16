import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamPost } from "../api";
import type { Invocation, Message, StreamEvent } from "../types";

/**
 * One piece of a turn as it happens. A turn is not "some text plus some tool
 * calls": the model writes, calls a tool, reads the result, writes again. Held
 * as a flat blob, the second round's analysis rendered above the tool call
 * that produced it — so the analyst read the conclusion before the evidence.
 */
export type LiveSegment =
  | { kind: "text"; key: string; text: string }
  | { kind: "reasoning"; key: string; text: string }
  | { kind: "tool"; key: string; invocation: Invocation };

export interface LiveState {
  segments: LiveSegment[];
  streaming: boolean;
}

const EMPTY_LIVE: LiveState = { segments: [], streaming: false };

/** Appends to the trailing segment of `kind`, or starts a new one. */
function appendText(
  segments: LiveSegment[],
  kind: "text" | "reasoning",
  text: string,
): LiveSegment[] {
  const last = segments[segments.length - 1];
  if (last && last.kind === kind) {
    return [...segments.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [...segments, { kind, key: `${kind}-${segments.length}`, text }];
}

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
    if (!conversationId) return null;
    const detail = await api.getConversation(conversationId);
    setMessages(detail.messages);
    setInvocations(detail.invocations);
    setTotalTokens(detail.total_tokens);
    setPending(detail.invocations.filter((i) => i.status === "pending_approval"));
    return detail;
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
          setLive((l) => ({ ...l, segments: appendText(l.segments, "text", event.text) }));
          break;
        case "reasoning":
          setLive((l) => ({
            ...l,
            segments: appendText(l.segments, "reasoning", event.text),
          }));
          break;
        case "tool_call":
          setLive((l) => ({
            ...l,
            segments: [
              ...l.segments,
              {
                kind: "tool",
                key: `tool-${event.invocation.id}`,
                invocation: event.invocation,
              },
            ],
          }));
          break;
        case "tool_result":
          setLive((l) => ({
            ...l,
            segments: l.segments.map((s) =>
              s.kind === "tool" && s.invocation.id === event.invocation.id
                ? { ...s, invocation: event.invocation }
                : s,
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
        const detail = await refresh();
        // The failure is now part of the transcript, so keeping the floating
        // banner too would show the analyst the same sentence twice.
        if (detail?.messages.some((m) => m.error)) setError(null);
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
        reasoning: null,
        error: null,
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
