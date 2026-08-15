import type { ComponentType } from "react";
import { IconAlert, IconFolder, IconKey, IconPulse } from "./components/Icons";

export interface QuickPrompt {
  /** Short label on the chip. */
  label: string;
  /** One line of context under the label on the big cards. */
  hint: string;
  /** What actually gets sent. Must stand on its own — these are one-click. */
  text: string;
  icon: ComponentType<{ size?: number; className?: string }>;
}

/**
 * The four questions an analyst opens a console to ask. Every one is
 * self-contained on purpose: a one-click prompt that makes the model come
 * back asking "which IP?" has cost a click rather than saved one.
 */
export const QUICK_PROMPTS: QuickPrompt[] = [
  {
    label: "Alerts, last 24 hours",
    hint: "Everything that fired, grouped by severity",
    text: "List all alerts from the past 24 hours, grouped by severity, with the affected assets and users for each.",
    icon: IconAlert,
  },
  {
    label: "Priority cases",
    hint: "Highest-severity cases still open",
    text: "Show me the highest-priority open cases, their current status, and what changed on each in the last day.",
    icon: IconFolder,
  },
  {
    label: "Noisiest detections",
    hint: "Which rules fired most this week",
    text: "Which detection rules fired most in the last 7 days? Include the hit count per rule and flag any that look like tuning candidates.",
    icon: IconPulse,
  },
  {
    label: "Failed logins",
    hint: "Repeated authentication failures",
    text: "Find failed login attempts from the last 24 hours and highlight any source addresses or accounts with repeated failures.",
    icon: IconKey,
  },
];
