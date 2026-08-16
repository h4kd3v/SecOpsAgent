import { useEffect, useRef, useState } from "react";
import { IconSparkle } from "./Icons";

interface Props {
  text: string;
  /** Open by default while it is still arriving — the point is to watch it. */
  live?: boolean;
}

/**
 * The model's working, when the gateway streams a reasoning channel.
 *
 * Kept visually distinct from the answer and collapsible: an analyst reviewing
 * a finished investigation wants the conclusion, but an analyst watching one
 * run wants to see what the model is considering before it commits to a tool
 * call. Defaults open while streaming, closed once the answer has landed.
 */
export function ReasoningBlock({ text, live = false }: Props) {
  const [open, setOpen] = useState(live);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (live && open && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [text, live, open]);

  if (!text.trim()) return null;

  return (
    <div className={`reasoning ${live ? "live" : ""}`}>
      <button className="reasoning-head" onClick={() => setOpen((o) => !o)}>
        <IconSparkle size={13} />
        <span>{live ? "Thinking…" : "Model's analysis"}</span>
        <span className="reasoning-toggle">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <div className="reasoning-body" ref={bodyRef}>
          {text}
        </div>
      )}
    </div>
  );
}
