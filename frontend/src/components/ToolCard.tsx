import { useState } from "react";
import { api } from "../api";
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
 * the trail.
 */
function summarise(args: Record<string, unknown>): string {
  for (const key of QUERY_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return Object.entries(args)
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join("  ");
}

interface Props {
  invocation: Invocation;
  /** Needed to fetch the rest of a result; absent while the turn is still live. */
  conversationId?: string | null;
}

/**
 * Analysts must be able to trace any claim back to the SecOps query that
 * produced it, so arguments and raw output are always one click away.
 *
 * The transcript carries only the first slice of a result. Chronicle returns
 * hundreds of kilobytes per page, and every turn ends in a reload of the whole
 * thread — shipping all of it made reopening an investigation a multi-megabyte
 * download of data already on screen. The rest is fetched when someone asks
 * for it, which is rare and cheap.
 */
export function ToolCard({ invocation, conversationId }: Props) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const preview = invocation.result_preview ?? "";
  const total = invocation.result_chars ?? preview.length;
  const shown = full ?? preview;
  const withheld = total - shown.length;
  const output = shown || invocation.error || "";
  const query = summarise(invocation.arguments ?? {});

  const loadRest = async () => {
    if (!conversationId) return;
    setLoading(true);
    setLoadError(null);
    try {
      const result = await api.invocationResult(conversationId, invocation.id);
      setFull(result.text);
    } catch (err) {
      setLoadError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

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
              <div className="tool-section-label">
                output
                {total > 0 && (
                  <span className="tool-size">
                    {" "}
                    · {total.toLocaleString()} characters
                  </span>
                )}
              </div>
              <pre>{output}</pre>
              {withheld > 0 && conversationId && (
                <button className="link-btn" disabled={loading} onClick={loadRest}>
                  {loading
                    ? "Loading…"
                    : `Show the remaining ${withheld.toLocaleString()} characters`}
                </button>
              )}
              {loadError && <div className="tool-load-error">{loadError}</div>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
