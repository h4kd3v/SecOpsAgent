import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Invocation, Message } from "../types";
import { ApprovalPanel } from "./ApprovalPanel";
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
  onSend: (text: string) => void;
  onDecide: (d: { invocation_id: string; decision: "approve" | "deny" }[]) => void;
  onStop: () => void;
}

export function ChatView(props: Props) {
  const { messages, invocations, live, pending, busy } = props;
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, live.assistantText, live.invocations.length, pending.length]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    props.onSend(text);
  };

  // `tool` rows are the model's view of the transcript; the analyst sees the
  // richer invocation card instead, anchored to the assistant turn that asked.
  const visible = messages.filter((m) => m.role === "user" || m.role === "assistant");

  return (
    <div className="chat">
      <div className="messages">
        {visible.length === 0 && !live.streaming && (
          <div className="empty-state">
            <h2>SecOps Agent</h2>
            <p>
              Ask about detections, alerts, entities or IOCs. Every answer is grounded in
              live SecOps data, and each tool call is shown with its arguments and output.
            </p>
          </div>
        )}

        {visible.map((message) => (
          <div key={message.id} className={`bubble ${message.role}`}>
            <div className="bubble-role">{message.role === "user" ? "You" : "Agent"}</div>
            <div className="bubble-body">
              {message.role === "assistant" ? (
                <Markdown remarkPlugins={[remarkGfm]}>{message.content ?? ""}</Markdown>
              ) : (
                message.content
              )}
            </div>
            {invocations
              .filter((i) => i.tool_call_id && messageOwnsCall(message, i))
              .map((i) => (
                <ToolCard key={i.id} invocation={i} />
              ))}
            {message.role === "assistant" && (
              <UsageFooter
                model={message.model}
                usage={message.token_usage}
                displayNameOverride={props.modelDisplayName}
              />
            )}
          </div>
        ))}

        {live.streaming && (
          <div className="bubble assistant">
            <div className="bubble-role">Agent</div>
            <div className="bubble-body">
              <Markdown remarkPlugins={[remarkGfm]}>{live.assistantText}</Markdown>
              {!live.assistantText && <span className="cursor">▊</span>}
            </div>
            {live.invocations.map((i) => (
              <ToolCard key={i.id} invocation={i} />
            ))}
          </div>
        )}

        {pending.length > 0 && (
          <ApprovalPanel invocations={pending} onDecide={props.onDecide} />
        )}

        {props.warning && <div className="warning-banner">{props.warning}</div>}
        {props.error && <div className="error-banner">{props.error}</div>}
        <div ref={bottomRef} />
      </div>

      {props.totalTokens > 0 && (
        <div className="conversation-total">
          {props.totalTokens.toLocaleString()} tokens used in this conversation
        </div>
      )}

      <div className="composer">
        <textarea
          value={draft}
          placeholder="Ask about an alert, entity, or detection…"
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
          <button className="btn-stop" onClick={props.onStop}>
            Stop
          </button>
        ) : (
          <button className="btn-send" onClick={submit} disabled={!draft.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}

function messageOwnsCall(message: Message, invocation: Invocation): boolean {
  const calls = (message.tool_calls ?? []) as { id?: string }[];
  return calls.some((c) => c.id === invocation.tool_call_id);
}
