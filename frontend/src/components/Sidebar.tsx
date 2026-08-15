import { useMemo, useState } from "react";
import type { Conversation, Session } from "../types";
import { IconChat, IconPlus, IconSearch, IconSettings, IconTrash } from "./Icons";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  session: Session;
  live: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: (id: string) => void;
  onClearAll: () => void;
  onOpenSettings: () => void;
}

/** Buckets a conversation by age, the way every chat sidebar does. */
function bucketOf(iso: string): string {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const start = midnight.getTime();
  const day = 86_400_000;
  const at = new Date(iso).getTime();

  if (at >= start) return "Today";
  if (at >= start - day) return "Yesterday";
  if (at >= start - 7 * day) return "Last 7 days";
  if (at >= start - 30 * day) return "Last 30 days";
  return "Older";
}

const BUCKET_ORDER = ["Today", "Yesterday", "Last 7 days", "Last 30 days", "Older"];

export function Sidebar({
  conversations,
  activeId,
  session,
  live,
  onSelect,
  onNew,
  onArchive,
  onClearAll,
  onOpenSettings,
}: Props) {
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle
      ? conversations.filter((c) => c.title.toLowerCase().includes(needle))
      : conversations;

    const byBucket = new Map<string, Conversation[]>();
    for (const conversation of matched) {
      const bucket = bucketOf(conversation.updated_at);
      const list = byBucket.get(bucket);
      if (list) list.push(conversation);
      else byBucket.set(bucket, [conversation]);
    }
    return BUCKET_ORDER.filter((b) => byBucket.has(b)).map(
      (b) => [b, byBucket.get(b)!] as const,
    );
  }, [conversations, query]);

  const initials = session.label
    .split(/\s+/)
    .map((word) => word[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <aside className="sidebar">
      <div className="brand">
        SECOPS<span className="brand-accent"> A.I</span>
      </div>

      <div className="sidebar-actions">
        <button className="btn-new" onClick={onNew}>
          <IconPlus size={17} />
          New chat
        </button>
        <button
          className={`btn-icon-dark ${searching ? "on" : ""}`}
          title="Search conversations"
          aria-label="Search conversations"
          onClick={() => {
            setSearching((s) => !s);
            setQuery("");
          }}
        >
          <IconSearch size={17} />
        </button>
      </div>

      {searching && (
        <input
          className="sidebar-search"
          autoFocus
          value={query}
          placeholder="Search conversations…"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setSearching(false);
              setQuery("");
            }
          }}
        />
      )}

      <div className="list-head">
        <span>Your conversations</span>
        {conversations.length > 0 && (
          <button className="link-btn" onClick={onClearAll}>
            Clear All
          </button>
        )}
      </div>

      <nav className="conversation-list">
        {activeId === null && (
          <div className="conversation active draft">
            <IconChat size={16} className="conversation-icon" />
            <span className="conversation-title">New chat</span>
            <span className="draft-hint">unsaved</span>
          </div>
        )}

        {groups.length === 0 && (query.trim() || activeId !== null) && (
          <p className="sidebar-empty">
            {query.trim() ? "Nothing matches that." : "No conversations yet."}
          </p>
        )}

        {groups.map(([bucket, items]) => (
          <div key={bucket} className="conversation-group">
            <div className="group-label">{bucket}</div>
            {items.map((conversation) => (
              <div
                key={conversation.id}
                className={`conversation ${conversation.id === activeId ? "active" : ""}`}
                onClick={() => onSelect(conversation.id)}
              >
                <IconChat size={16} className="conversation-icon" />
                <span className="conversation-title">{conversation.title}</span>
                <button
                  className="conversation-archive"
                  title="Archive this conversation"
                  aria-label="Archive this conversation"
                  onClick={(e) => {
                    e.stopPropagation();
                    onArchive(conversation.id);
                  }}
                >
                  <IconTrash size={15} />
                </button>
              </div>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <button className="foot-row" onClick={onOpenSettings}>
          <IconSettings size={17} />
          <span>Settings</span>
        </button>

        <div className="foot-row static">
          <span className="avatar">{initials || "A"}</span>
          <span className="who-label">{session.label}</span>
          <span
            className={`live-dot ${live ? "on" : "off"}`}
            title={live ? "Live — history syncing" : "Reconnecting…"}
          />
        </div>
      </div>
    </aside>
  );
}
