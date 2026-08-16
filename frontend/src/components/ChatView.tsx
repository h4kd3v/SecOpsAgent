import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Invocation, Message } from "../types";
import { ApprovalPanel } from "./ApprovalPanel";
import {
  IconAlert,
  IconCheck,
  IconCopy,
  IconRetry,
  IconSend,
  IconSparkle,
  IconStop,
} from "./Icons";
import { PendingToolCard } from "./PendingToolCard";
import { ReasoningBlock } from "./ReasoningBlock";
import { PromptCards, PromptChips } from "./SuggestedPrompts";
import { ToolCard } from "./ToolCard";
import { UsageFooter } from "./UsageFooter";
import type { LiveState } from "../hooks/useChatStream";

interface Props {
  messages: Message[];
  invocations: Invocation[];
  live: LiveState;
  pending: Invocation[];
  busy: boolean;
  error: string | null;
  warning: string | null;
  totalTokens: number;
  modelDisplayName: string;
  sessionInitials: string;
  onSend: (text: string) => void;
  onDecide: (d: { invocation_id: string; decision: "approve" | "deny" }[]) => void;
  onStop: () => void;
}

export function ChatView(props: Props) {
  const { messages, invocations, live, pending, busy } = props;
  const [draft, setDraft] = useState("");
  const [copied, setCopied] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, liveLength(live.segments), live.segments.length, pending.length]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    props.onSend(text);
  };

  const pick = (text: string) => {
    if (busy) return;
    props.onSend(text);
  };

  const copy = (id: string, text: string) => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(id);
      window.setTimeout(() => setCopied((c) => (c === id ? null : c)), 1600);
    });
  };

  // `tool` rows are the model's view of the transcript; the analyst sees the
  // richer invocation card instead, anchored to the assistant turn that asked.
  const visible = messages.filter((m) => m.role === "user" || m.role === "assistant");
  const lastUserPrompt = [...visible].reverse().find((m) => m.role === "user")?.content ?? "";
  const lastId = visible[visible.length - 1]?.id;
  const empty = visible.length === 0 && !live.streaming;

  return (
    <div className="chat">
      <div className="messages">
        {empty && (
          <div className="empty-state">
            <span className="empty-mark">
              <IconSparkle size={26} />
            </span>
            <h2>How can I help with your investigation?</h2>
            <p>
              Ask about detections, alerts, entities or IOCs. Every answer is grounded in
              live SecOps data, and each tool call is shown with its arguments and output.
            </p>
            <PromptCards onPick={pick} disabled={busy} />
          </div>
        )}

        {visible.map((message) => (
          <div key={message.id} className={`turn ${message.role}`}>
            {message.role === "user" ? (
              <div className="turn-head">
                <span className="avatar user-avatar">{props.sessionInitials}</span>
                <div className="user-text">{message.content}</div>
                <button
                  className="turn-action"
                  title="Copy prompt"
                  aria-label="Copy prompt"
                  onClick={() => copy(message.id, message.content ?? "")}
                >
                  {copied === message.id ? <IconCheck size={15} /> : <IconCopy size={15} />}
                </button>
              </div>
            ) : (
              <>
                {/* A turn that only carried tool calls has no prose. Rendering
                    the label and an empty card for it leaves a blank white
                    strip above the tool card that reads as a broken answer. */}
                {(hasText(message) || message.reasoning) && (
                  <div className="agent-label">
                    <IconSparkle size={13} />
                    SecOps Agent
                  </div>
                )}
                {message.reasoning && <ReasoningBlock text={message.reasoning} />}
                {hasText(message) && (
                  <div className="agent-body">
                    <Markdown remarkPlugins={[remarkGfm]}>{message.content ?? ""}</Markdown>
                  </div>
                )}
                {invocations
                  .filter((i) => i.tool_call_id && messageOwnsCall(message, i))
                  .map((i) => (
                    <ToolCard key={i.id} invocation={i} />
                  ))}
                {message.status === "cancelled" && (
                  <div className="stopped-note">
                    <IconStop size={12} />
                    {hasText(message)
                      ? "You stopped this answer — it is incomplete."
                      : "You stopped this turn before the model replied."}
                  </div>
                )}
                {/* Read from the message, not from the live stream, so it is
                    still here when the conversation is reopened tomorrow. */}
                {message.error && (
                  <div className="turn-error">
                    <IconAlert size={15} />
                    <span>{message.error}</span>
                  </div>
                )}
                {hasText(message) && (
                <div className="turn-footer">
                  <div className="turn-actions">
                    <button
                      className="turn-action"
                      title="Copy answer"
                      aria-label="Copy answer"
                      onClick={() => copy(message.id, message.content ?? "")}
                    >
                      {copied === message.id ? <IconCheck size={15} /> : <IconCopy size={15} />}
                    </button>
                    {message.id === lastId && lastUserPrompt && (
                      <button
                        className="turn-action labelled"
                        title="Ask the same question again"
                        disabled={busy}
                        onClick={() => pick(lastUserPrompt)}
                      >
                        <IconRetry size={15} />
                        Retry
                      </button>
                    )}
                  </div>
                  <UsageFooter
                    model={message.model}
                    usage={message.token_usage}
                    displayNameOverride={props.modelDisplayName}
                  />
                </div>
                )}
              </>
            )}
          </div>
        ))}

        {live.streaming && (
          <div className="turn assistant">
            <div className="agent-label">
              <IconSparkle size={13} />
              SecOps Agent
            </div>
            {/* Rendered in arrival order. The model writes, calls a tool, reads
                the result and writes again; flattening that into one bubble
                put later analysis above the tool call it came from. */}
            {live.segments.map((segment) =>
              segment.kind === "tool" ? (
                <ToolCard key={segment.key} invocation={segment.invocation} />
              ) : segment.kind === "draft" ? (
                <PendingToolCard
                  key={segment.key}
                  name={segment.name}
                  arguments={segment.args}
                />
              ) : segment.kind === "reasoning" ? (
                <ReasoningBlock key={segment.key} text={segment.text} live />
              ) : (
                <div className="agent-body" key={segment.key}>
                  <Markdown remarkPlugins={[remarkGfm]}>{segment.text}</Markdown>
                </div>
              ),
            )}
            {live.segments.length === 0 && (
              <div className="agent-body">
                <span className="cursor" />
              </div>
            )}
          </div>
        )}

        {pending.length > 0 && (
          <ApprovalPanel invocations={pending} onDecide={props.onDecide} />
        )}

        {props.warning && <div className="warning-banner">{props.warning}</div>}
        {props.error && <div className="error-banner">{props.error}</div>}
        <div ref={bottomRef} />
      </div>

      <div className="composer-dock">
        {props.totalTokens > 0 && (
          <div className="conversation-total">
            {props.totalTokens.toLocaleString()} tokens used in this conversation
          </div>
        )}

        {!empty && <PromptChips onPick={pick} disabled={busy} />}

        <div className="composer">
          <span className="composer-mark">
            <IconSparkle size={16} />
          </span>
          <textarea
            value={draft}
            placeholder="What's on your mind?…"
            rows={1}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          {busy ? (
            <button className="btn-round stop" onClick={props.onStop} title="Stop">
              <IconStop size={18} />
            </button>
          ) : (
            <button
              className="btn-round"
              onClick={submit}
              disabled={!draft.trim()}
              title="Send"
            >
              <IconSend size={18} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Total characters streamed so far — the scroll trigger while a turn runs. */
function liveLength(segments: LiveState["segments"]): number {
  return segments.reduce(
    (n, s) =>
      n + (s.kind === "tool" ? 0 : s.kind === "draft" ? s.args.length : s.text.length),
    0,
  );
}

function hasText(message: Message): boolean {
  return (message.content ?? "").trim().length > 0;
}

function messageOwnsCall(message: Message, invocation: Invocation): boolean {
  const calls = (message.tool_calls ?? []) as { id?: string }[];
  return calls.some((c) => c.id === invocation.tool_call_id);
}
