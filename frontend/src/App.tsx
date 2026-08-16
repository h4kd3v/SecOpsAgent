import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ChatView } from "./components/ChatView";
import { IconTools } from "./components/Icons";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { ToolsPanel } from "./components/ToolsPanel";
import { useChatStream } from "./hooks/useChatStream";
import { useSidebar } from "./hooks/useSidebar";
import type { AppConfig, Session } from "./types";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [queued, setQueued] = useState<string | null>(null);
  const [showTools, setShowTools] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  // No login screen: the first request mints an anonymous session and drops a
  // signed cookie. Returning visitors get their existing sidebar back.
  useEffect(() => {
    api
      .openSession()
      .then(async (opened) => {
        setSession(opened);
        // Cosmetic only; a missing config must not block the app.
        setConfig(await api.config().catch(() => null));
      })
      .catch((err) => setBootError((err as Error).message));
  }, []);

  const sidebar = useSidebar(session !== null);

  const onTitle = useCallback(() => {
    // The title also arrives over /api/events; nothing to do here.
  }, []);

  const chat = useChatStream(activeId, onTitle);

  // Deliberately does NOT create anything. A conversation is a container for
  // messages; one with no messages is a row nobody asked for. `send()` below
  // creates it on the first message instead, so clicking this repeatedly
  // leaves no trace.
  const newConversation = () => {
    setActiveId(null);
    setQueued(null);
  };

  const archive = async (id: string) => {
    await api.archiveConversation(id);
    if (activeId === id) setActiveId(null);
  };


  const send = async (text: string) => {
    // First message of a fresh session: create the thread, then let the effect
    // below fire once `useChatStream` has rebound to the new conversation id.
    if (!activeId) {
      const conversation = await api.createConversation();
      setActiveId(conversation.id);
      setQueued(text);
      return;
    }
    await chat.send(text);
  };

  useEffect(() => {
    if (!queued || !activeId) return;
    setQueued(null);
    void chat.send(queued);
  }, [queued, activeId, chat]);

  if (bootError) {
    return (
      <div className="boot">
        Couldn't start a session: {bootError}
        <br />
        Check that the backend is reachable, then reload.
      </div>
    );
  }
  if (!session) return <div className="boot">Starting…</div>;

  const initials =
    session.label
      .split(/\s+/)
      .map((word) => word[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "A";

  return (
    <div className="app">
      <div className="shell">
        <Sidebar
          conversations={sidebar.conversations}
          activeId={activeId}
          session={session}
          live={sidebar.live}
          onSelect={setActiveId}
          onNew={newConversation}
          onArchive={archive}
          onOpenSettings={() => setShowSettings(true)}
        />
        <ChatView
          messages={chat.messages}
          invocations={chat.invocations}
          live={chat.live}
          pending={chat.pending}
          busy={chat.busy}
          error={chat.error}
          warning={chat.warning}
          totalTokens={chat.totalTokens}
          modelDisplayName={config?.model_display_name ?? ""}
          sessionInitials={initials}
          onSend={send}
          onDecide={chat.decide}
          onStop={chat.stop}
        />
        <button className="tools-tab" onClick={() => setShowTools(true)}>
          <IconTools size={16} />
          <span>MCP Tools</span>
        </button>
      </div>

      {showTools && <ToolsPanel onClose={() => setShowTools(false)} />}
      {showSettings && (
        <SettingsPanel
          config={config}
          session={session}
          onClose={() => setShowSettings(false)}
        />
      )}
    </div>
  );
}
