import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ToolCatalog } from "../types";

function humanAge(seconds: number | null): string {
  if (seconds == null) return "unknown";
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

/**
 * Lists what the MCP server exposes and how each tool is classified.
 * Definitions are served from the cached catalogue, so this is instant and
 * keeps working while the MCP server is briefly down.
 */
export function ToolsPanel({ onClose }: { onClose: () => void }) {
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (force: boolean) => {
    setBusy(true);
    setError(null);
    try {
      setCatalog(await (force ? api.refreshTools() : api.listTools()));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const writes = catalog?.tools.filter((t) => !t.read_only).length ?? 0;

  return (
    <div className="panel-backdrop" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>MCP tools</h2>
          <div className="panel-actions">
            <button className="btn-refresh" onClick={() => load(true)} disabled={busy}>
              {busy ? "Refreshing…" : "Refresh"}
            </button>
            <button className="panel-close" onClick={onClose}>
              ×
            </button>
          </div>
        </div>

        {error && (
          <div className="error-banner panel-error">
            <strong>Could not reach the MCP server.</strong>
            <pre>{error}</pre>
            Run <code>docker compose exec backend python -m app.diagnose</code> for a
            full connectivity check.
          </div>
        )}

        {catalog?.stale && (
          <div className="warning-banner panel-error">
            Showing a cached copy from {humanAge(catalog.age_seconds)} — the MCP server
            could not be reached to refresh it.
            {catalog.error && <pre>{catalog.error}</pre>}
          </div>
        )}

        {!catalog && !error && <p className="panel-dim">Loading…</p>}

        {catalog && (
          <>
            <p className="panel-dim">
              {catalog.tools.length} tools · {catalog.tools.length - writes} read-only ·{" "}
              {writes} need approval
            </p>
            <p className="panel-dim">
              Definitions {catalog.source === "live" ? "fetched" : "cached"}{" "}
              {humanAge(catalog.age_seconds)}, refreshed automatically every{" "}
              {catalog.ttl_hours} h.
            </p>
            <ul className="tool-list">
              {catalog.tools.map((tool) => (
                <li key={tool.name}>
                  <div className="tool-list-head">
                    <code>{tool.name}</code>
                    <span className={`badge ${tool.read_only ? "badge-read" : "badge-write"}`}>
                      {tool.read_only ? "read" : "write"}
                    </span>
                  </div>
                  {tool.description !== tool.name && (
                    <p className="tool-list-desc">{tool.description}</p>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
