import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Tool, ToolCatalog } from "../types";
import { IconClose, IconSearch } from "./Icons";

/** Chronicle ships tool descriptions running to thousands of characters —
 *  full usage guidelines, response templates, worked examples. Useful when
 *  you want them, unreadable as a list of 70. */
const PREVIEW_CHARS = 260;

function ToolRow({ tool }: { tool: Tool }) {
  const [open, setOpen] = useState(false);
  const description = tool.description === tool.name ? "" : tool.description;
  const long = description.length > PREVIEW_CHARS;

  return (
    <li>
      <div className="tool-list-head">
        <code>{tool.name}</code>
        <span className={`badge ${tool.read_only ? "badge-read" : "badge-write"}`}>
          {tool.read_only ? "read" : "write"}
        </span>
      </div>
      {description && (
        <p className="tool-list-desc">
          {long && !open ? `${description.slice(0, PREVIEW_CHARS).trimEnd()}… ` : description}
          {long && (
            <button className="link-btn inline" onClick={() => setOpen((o) => !o)}>
              {open ? "less" : "more"}
            </button>
          )}
        </p>
      )}
    </li>
  );
}

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
  const [query, setQuery] = useState("");

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

  // Descriptions are searched too — an analyst looking for "detection rule"
  // should find `list_rules` without knowing it is called that — but they are
  // long enough that a common word like "case" matches almost everything. So
  // name matches rank above description-only ones, which puts `get_case` and
  // `list_cases` at the top instead of somewhere in a list of 64.
  const matches = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!catalog) return [];
    if (!terms.length) return catalog.tools;

    const scored: { tool: Tool; byName: boolean }[] = [];
    for (const tool of catalog.tools) {
      const name = tool.name.toLowerCase();
      const all = `${name} ${tool.description.toLowerCase()}`;
      if (!terms.every((term) => all.includes(term))) continue;
      scored.push({ tool, byName: terms.every((term) => name.includes(term)) });
    }
    return scored
      .sort((a, b) => Number(b.byName) - Number(a.byName))
      .map((entry) => entry.tool);
  }, [catalog, query]);

  const named = useMemo(() => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return 0;
    return matches.filter((t) => terms.every((term) => t.name.toLowerCase().includes(term)))
      .length;
  }, [matches, query]);

  return (
    <div className="panel-backdrop" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>MCP tools</h2>
          <div className="panel-actions">
            <button className="btn-refresh" onClick={() => load(true)} disabled={busy}>
              {busy ? "Refreshing…" : "Refresh"}
            </button>
            <button className="panel-close" onClick={onClose} aria-label="Close">
              <IconClose size={18} />
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
            <div className="tool-search">
              <IconSearch size={16} />
              <input
                autoFocus
                value={query}
                placeholder="Search tools by name or description…"
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && setQuery("")}
              />
              {query && (
                <button className="link-btn" onClick={() => setQuery("")}>
                  clear
                </button>
              )}
            </div>

            <p className="panel-dim">
              {query
                ? `${matches.length} of ${catalog.tools.length} tools match` +
                  (named && named < matches.length ? ` · ${named} by name, listed first` : "")
                : `${catalog.tools.length} tools · ${catalog.tools.length - writes} read-only · ${writes} need approval`}
            </p>
            <p className="panel-dim">
              Definitions {catalog.source === "live" ? "fetched" : "cached"}{" "}
              {humanAge(catalog.age_seconds)}, refreshed automatically every{" "}
              {catalog.ttl_hours} h.
            </p>

            {matches.length === 0 && (
              <p className="panel-dim">Nothing matches “{query}”.</p>
            )}
            <ul className="tool-list">
              {matches.map((tool) => (
                <ToolRow key={tool.name} tool={tool} />
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
