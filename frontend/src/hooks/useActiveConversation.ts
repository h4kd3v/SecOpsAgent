import { useCallback, useEffect, useState } from "react";

const ROUTE = /^#\/c\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;

function fromUrl(): string | null {
  const match = window.location.hash.match(ROUTE);
  return match ? match[1].toLowerCase() : null;
}

/**
 * Which conversation is open, kept in the URL.
 *
 * Holding it in React state alone meant a refresh dropped the analyst back on
 * an empty "New chat" screen with their work still in the sidebar but no
 * longer in front of them — worst right after an interruption, when what they
 * want is to see how far the answer got.
 *
 * The hash is deliberate rather than a path: nginx serves this as a static
 * bundle, and a real path would 404 on reload unless every route were rewritten
 * server-side. `replaceState` rather than `pushState` so Back leaves the app
 * instead of walking backwards through a session's threads.
 */
export function useActiveConversation(): [string | null, (id: string | null) => void] {
  const [activeId, setActiveId] = useState<string | null>(fromUrl);

  const select = useCallback((id: string | null) => {
    setActiveId(id);
    const next = id ? `#/c/${id}` : "";
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", next || window.location.pathname);
    }
  }, []);

  // Keeps a hand-edited URL, or a Back that lands on a different hash, honest.
  useEffect(() => {
    const sync = () => setActiveId(fromUrl());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  return [activeId, select];
}
