import { IconTools } from "./Icons";

interface Props {
  name: string;
  /** Raw JSON as far as the model has written it — usually not yet parseable. */
  arguments: string;
}

/**
 * A tool call while the model is still composing it.
 *
 * The query is model output like any other, and it is the most revealing part
 * of an investigation: which question the model decided to ask SecOps. Waiting
 * for the round to finish before showing it leaves the analyst watching a
 * spinner through the part they most want to see.
 *
 * Replaced by the real ToolCard the moment the call is authorised and run.
 */
export function PendingToolCard({ name, arguments: args }: Props) {
  return (
    <div className="tool-card pending">
      <div className="tool-head as-row">
        <IconTools size={14} className="tool-chevron" />
        <code>{name || "…"}</code>
        <span className="tool-status">composing</span>
      </div>
      {args && (
        <div className="tool-body">
          <div className="tool-section-label">query</div>
          <pre>
            {args}
            <span className="cursor inline" />
          </pre>
        </div>
      )}
    </div>
  );
}
