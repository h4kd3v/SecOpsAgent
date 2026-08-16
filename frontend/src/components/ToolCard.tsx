import { useState } from "react";
import type { Invocation } from "../types";

const LABELS: Record<Invocation["status"], string> = {
  pending_approval: "awaiting approval",
  denied: "declined",
  running: "running",
  succeeded: "ok",
  failed: "failed",
  timeout: "timed out",
  cancelled: "stopped",
};

/** Keys that hold the actual question, in rough order of how SecOps names them. */
const QUERY_KEYS = ["query", "udm_query", "q", "search", "filter", "expression"];

/**
 * A one-line rendering of what the model actually asked for.
 *
 * The analyst's first question about any tool call is "what did it search
 * for?", and burying that behind a toggle means expanding every card to read
 * the trail. The raw *result* stays collapsed — that is bulk data for the
 * model, not something to dump into the conversation.
 */
function summarise(args: Record<string, unknown>): string {
  for (const key of QUERY_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  const parts = Object.entries(args).map(
    ([key, value]) =>
      `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`,
  );
  return parts.join("  ");
}

/**
 * Analysts must be able to trace any claim back to the SecOps query that
 * produced it, so arguments and raw output are always one click away.
 */
export function ToolCard({ invocation }: { invocation: Invocation }) {
  const [open, setOpen] = useState(false);
  const output = invocation.result?.text ?? invocation.result_preview ?? invocation.error ?? "";
  const query = summarise(invocation.arguments ?? {});

  return (
    <div className={`tool-card status-${invocation.status}`}>
      <button className="tool-head" onClick={() => setOpen((o) => !o)}>
        <span className="tool-chevron">{open ? "▾" : "▸"}</span>
        <code>{invocation.tool_name}</code>
        {invocation.is_write && <span className="badge badge-write">write</span>}
        <span className="tool-status">{LABELS[invocation.status]}</span>
        {invocation.latency_ms != null && (
          <span className="tool-latency">{invocation.latency_ms} ms</span>
        )}
      </button>

      {query && !open && <div className="tool-query">{query}</div>}

      {open && (
        <div className="tool-body">
          <div className="tool-section-label">arguments</div>
          <pre>{JSON.stringify(invocation.arguments, null, 2)}</pre>
          {output && (
            <>
              <div className="tool-section-label">output</div>
              <pre>{output}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
