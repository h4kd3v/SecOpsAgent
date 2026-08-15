import { QUICK_PROMPTS } from "../prompts";

interface Props {
  onPick: (text: string) => void;
  disabled: boolean;
}

/** The 2x2 grid on an empty conversation. */
export function PromptCards({ onPick, disabled }: Props) {
  return (
    <div className="prompt-cards">
      {QUICK_PROMPTS.map(({ label, hint, text, icon: Icon }) => (
        <button
          key={label}
          className="prompt-card"
          disabled={disabled}
          title={text}
          onClick={() => onPick(text)}
        >
          <span className="prompt-card-icon">
            <Icon size={18} />
          </span>
          <span className="prompt-card-text">
            <strong>{label}</strong>
            <span>{hint}</span>
          </span>
        </button>
      ))}
    </div>
  );
}

/**
 * The same prompts as a compact strip above the composer, so they stay one
 * click away once a conversation is under way — that is when an analyst most
 * often wants a standing question, not on a blank screen.
 */
export function PromptChips({ onPick, disabled }: Props) {
  return (
    <div className="prompt-chips">
      {QUICK_PROMPTS.map(({ label, text, icon: Icon }) => (
        <button
          key={label}
          className="prompt-chip"
          disabled={disabled}
          title={text}
          onClick={() => onPick(text)}
        >
          <Icon size={14} />
          {label}
        </button>
      ))}
    </div>
  );
}
