import type { TokenUsage } from "../types";

/** Strips the noise from a gateway model id: `openai/claude-opus-4-6-20260101`
 *  reads better as `claude-opus-4-6`. Only cosmetic — set
 *  LLM_MODEL_DISPLAY_NAME for a properly branded label like "Claude Opus 4.6". */
function tidy(model: string): string {
  const withoutVendor = model.includes("/") ? model.split("/").pop()! : model;
  return withoutVendor.replace(/-\d{8}$/, "");
}

/** $0.0031 rather than $0.00: a turn that cost a third of a cent did not cost
 *  nothing, and a month of them is real money. */
function money(amount: string): string | null {
  const value = Number(amount);
  if (!Number.isFinite(value) || value <= 0) return null;
  if (value >= 0.01) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(4)}`;
}

interface Props {
  model: string | null;
  usage: TokenUsage | null;
  cost?: string | null;
  displayNameOverride: string;
}

export function UsageFooter({ model, usage, cost, displayNameOverride }: Props) {
  if (!model && !usage) return null;
  const spent = cost ? money(cost) : null;

  const label = displayNameOverride || (model ? tidy(model) : "");
  const total = usage?.total_tokens;
  // Some gateways omit usage on streamed responses, so the backend falls back
  // to a heuristic. Mark it, rather than passing a guess off as a measurement.
  const prefix = usage?.estimated ? "~" : "";

  const breakdown =
    usage && usage.prompt_tokens != null
      ? `${usage.prompt_tokens.toLocaleString()} in · ${(
          usage.completion_tokens ?? 0
        ).toLocaleString()} out${usage.estimated ? " (estimated)" : ""}`
      : undefined;

  return (
    <div className="usage-footer" title={breakdown}>
      {label && <span className="usage-model">{label}</span>}
      {label && total != null && <span className="usage-dot">·</span>}
      {total != null && (
        <span className="usage-tokens">
          {prefix}
          {total.toLocaleString()} tokens
        </span>
      )}
      {spent && <span className="usage-dot">·</span>}
      {spent && (
        <span className="usage-cost" title="At the rates in force when this ran">
          {prefix}
          {spent}
        </span>
      )}
    </div>
  );
}
