import { useState } from "react";
import type { Invocation } from "../types";

const LABELS: Record<Invocation["status"], string> = {
  pending_approval: "awaiting approval",
  denied: "declined",
  running: "running",
  succeeded: "ok",
  failed: "failed",
  timeout: "timed out",
};

/**
 * Analysts must be able to trace any claim back to the SecOps query that
 * produced it, so arguments and raw output are always one click away.
 */
export function ToolCard({ invocation }: { invocation: Invocation }) {
  const [open, setOpen] = useState(false);
  const output = invocation.result?.text ?? invocation.result_preview ?? invocation.error ?? "";

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
