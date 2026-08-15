import type { Conversation, Session } from "../types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  session: Session;
  live: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onArchive: (id: string) => void;
  onShowTools: () => void;
}

export function Sidebar({
  conversations,
  activeId,
  session,
  live,
  onSelect,
  onNew,
  onArchive,
  onShowTools,
}: Props) {
  return (
    <aside className="sidebar">
      <button className="btn-new" onClick={onNew}>
        + New investigation
      </button>

      <nav className="conversation-list">
        {activeId === null && (
          <div className="conversation active draft">
            <span className="conversation-title">New investigation</span>
            <span className="draft-hint">unsaved</span>
          </div>
        )}
        {conversations.length === 0 && activeId !== null && (
          <p className="sidebar-empty">No investigations yet.</p>
        )}
        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            className={`conversation ${conversation.id === activeId ? "active" : ""}`}
            onClick={() => onSelect(conversation.id)}
          >
            <span className="conversation-title">{conversation.title}</span>
            <button
              className="conversation-archive"
              title="Archive"
              onClick={(e) => {
                e.stopPropagation();
                onArchive(conversation.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <button className="btn-tools" onClick={onShowTools}>
        MCP tools
      </button>

      <div className="sidebar-footer">
        <div className="who">
          <span
            className={`live-dot ${live ? "on" : "off"}`}
            title={live ? "Live — sidebar syncing" : "Reconnecting…"}
          />
          <span>{session.label}</span>
        </div>
        <p className="sidebar-note">
          History is tied to this browser. Clearing cookies starts a new one.
        </p>
      </div>
    </aside>
  );
}
