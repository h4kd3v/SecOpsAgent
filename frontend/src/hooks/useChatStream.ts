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
  | { kind: "tool"; key: string; invocation: Invocation }
  // A tool call the model is still writing. Replaced by the real card as soon
  // as the call is authorised and dispatched.
  | { kind: "draft"; key: string; index: number; name: string; args: string };

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

export function useChatStream(
  conversationId: string | null,
  onTitle: (t: string) => void,
  onMissing?: () => void,
) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [invocations, setInvocations] = useState<Invocation[]>([]);
  const [totalTokens, setTotalTokens] = useState(0);
  const [live, setLive] = useState<LiveState>(EMPTY_LIVE);
  const [pending, setPending] = useState<Invocation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Held in a ref so `refresh` keeps a stable identity: it is a dependency of
  // the effect below and of `drive`.
  const missingRef = useRef(onMissing);
  useEffect(() => {
    missingRef.current = onMissing;
  }, [onMissing]);

  const refresh = useCallback(async () => {
    if (!conversationId) return null;
    let detail;
    try {
      detail = await api.getConversation(conversationId);
    } catch (err) {
      // A conversation id can outlive the thread it names — restored from the
      // URL after it was archived, or typed in by hand. The API answers 404
      // for anything this session does not own, so treat both the same and
      // fall back to a new chat rather than leaving a dead screen.
      setMessages([]);
      setInvocations([]);
      setTotalTokens(0);
      setPending([]);
      missingRef.current?.();
      return null;
    }
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

  // Reloading mid-turn races the server's cleanup: the fresh page can fetch
  // the transcript in the time it takes the backend to notice the disconnect
  // and settle the row. Without this the analyst is left looking at a
  // half-written answer with nothing to say why it stopped. Bounded, because
  // a row can also be legitimately streaming in another tab.
  const unsettled = messages.some((m) => m.status === "streaming");
  useEffect(() => {
    if (busy || !unsettled) return;
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      void refresh();
      if (attempts >= 3) window.clearInterval(timer);
    }, 900);
    return () => window.clearInterval(timer);
  }, [busy, unsettled, refresh]);

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
        case "tool_call_delta":
          setLive((l) => {
            const at = l.segments.findIndex(
              (s) => s.kind === "draft" && s.index === event.index,
            );
            const draft: LiveSegment = {
              kind: "draft",
              key: `draft-${event.index}`,
              index: event.index,
              name: event.name,
              args: event.arguments,
            };
            if (at === -1) return { ...l, segments: [...l.segments, draft] };
            const segments = [...l.segments];
            segments[at] = draft;
            return { ...l, segments };
          });
          break;
        case "tool_call":
          setLive((l) => ({
            ...l,
            // Drafts for this round are superseded the moment real invocation
            // rows exist; keeping both would show the same call twice.
            segments: [
              ...l.segments.filter((s) => s.kind !== "draft"),
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
        author_label: null,
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
