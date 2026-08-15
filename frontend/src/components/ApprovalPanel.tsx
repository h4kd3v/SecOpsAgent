import { useState } from "react";
import type { Invocation } from "../types";

interface Props {
  invocations: Invocation[];
  onDecide: (decisions: { invocation_id: string; decision: "approve" | "deny" }[]) => void;
}

/**
 * The gate between a model that suggests changes and a platform that makes
 * them. Nothing state-changing reaches SecOps without passing through here.
 */
export function ApprovalPanel({ invocations, onDecide }: Props) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});

  const submit = (approveAll: boolean | null) => {
    onDecide(
      invocations.map((i) => ({
        invocation_id: i.id,
        decision:
          approveAll === null
            ? chosen[i.id]
              ? "approve"
              : "deny"
            : approveAll
              ? "approve"
              : "deny",
      })),
    );
  };

  return (
    <div className="approval-panel">
      <div className="approval-title">
        The model wants to run {invocations.length} state-changing action
        {invocations.length > 1 ? "s" : ""}
      </div>

      {invocations.map((inv) => (
        <label key={inv.id} className="approval-row">
          <input
            type="checkbox"
            checked={!!chosen[inv.id]}
            onChange={(e) => setChosen((c) => ({ ...c, [inv.id]: e.target.checked }))}
          />
          <div>
            <code>{inv.tool_name}</code>
            <pre>{JSON.stringify(inv.arguments, null, 2)}</pre>
          </div>
        </label>
      ))}

      <div className="approval-actions">
        <button className="btn-approve" onClick={() => submit(null)}>
          Run selected
        </button>
        <button className="btn-deny" onClick={() => submit(false)}>
          Decline all
        </button>
      </div>
      <p className="approval-note">
        Every decision is recorded in the audit trail against this session.
      </p>
    </div>
  );
}
