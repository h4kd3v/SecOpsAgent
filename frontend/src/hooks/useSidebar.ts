import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Conversation, SidebarEvent } from "../types";

/**
 * Keeps the conversation list live off `/api/events`.
 *
 * Events reach us via Postgres NOTIFY, so a thread created or retitled in one
 * tab appears in the others immediately — and correctly even when the tabs are
 * served by different backend workers.
 *
 * EventSource is the right client here: the stream is a plain authenticated
 * GET, and the browser handles reconnect-with-backoff for free.
 */
export function useSidebar(enabled: boolean) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [live, setLive] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    setConversations(await api.listConversations());
  }, []);

  // Held in a ref so `apply` has a stable identity — otherwise the effect
  // below would tear down and rebuild the EventSource on every render.
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  const apply = useCallback((event: SidebarEvent) => {
    if (event.type === "resync") {
      void refreshRef.current?.();
      return;
    }
    const incoming = event.conversation;
    setConversations((list) => {
      if (event.type === "conversation_archived") {
        return list.filter((c) => c.id !== incoming.id);
      }
      const without = list.filter((c) => c.id !== incoming.id);
      const merged = [...without, incoming];
      // Server orders by updated_at desc; mirror that so positions match
      // after a refresh.
      merged.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
      return merged;
    });
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void refresh();

    const source = new EventSource("/api/events", { withCredentials: true });
    sourceRef.current = source;

    const handler = (e: MessageEvent) => {
      try {
        apply(JSON.parse(e.data) as SidebarEvent);
      } catch {
        /* a truncated frame means the connection died mid-write */
      }
    };

    for (const name of [
      "resync",
      "conversation_created",
      "conversation_updated",
      "conversation_archived",
    ]) {
      source.addEventListener(name, handler as EventListener);
    }
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false); // EventSource reconnects on its own

    return () => {
      source.close();
      sourceRef.current = null;
      setLive(false);
    };
  }, [enabled, apply, refresh]);

  return { conversations, live, refresh, setConversations };
}
