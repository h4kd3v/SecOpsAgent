/**
 * Inline SVG icons.
 *
 * Deliberately not an icon package: the CSP forbids external requests, and a
 * dozen 24px glyphs are cheaper to inline than to ship a font or a dependency
 * that would need pinning and auditing like any other supply-chain input.
 */
const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

interface Props {
  size?: number;
  className?: string;
}

const wrap = (size = 16, className?: string) => ({
  ...base,
  width: size,
  height: size,
  className,
  "aria-hidden": true,
});

export const IconPlus = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconSearch = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.2-3.2" />
  </svg>
);

export const IconChat = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M20 15a3 3 0 0 1-3 3H9l-4 3v-3a3 3 0 0 1-1-2.2V7a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z" />
  </svg>
);

export const IconTrash = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M4 7h16M10 7V5h4v2M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" />
  </svg>
);

export const IconCopy = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <rect x="9" y="9" width="11" height="11" rx="2.5" />
    <path d="M15 5.5A2.5 2.5 0 0 0 12.5 3h-7A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15" />
  </svg>
);

export const IconCheck = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="m4.5 12.5 5 5 10-11" />
  </svg>
);

export const IconRetry = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M20 11a8 8 0 1 0-.7 4.3" />
    <path d="M20 5v6h-6" />
  </svg>
);

export const IconSettings = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 14a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.3a2 2 0 1 1-4 0v-.2a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3.5 13H3a2 2 0 1 1 0-4h.2A1.6 1.6 0 0 0 4.3 6.2l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 10 2.5V3a2 2 0 1 1 4 0v.2a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.4a2 2 0 1 1 0 4h-.2a1.6 1.6 0 0 0-1.4 1.2z" />
  </svg>
);

export const IconTools = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M14.7 6.3a4 4 0 0 0 5.2 5.2L21 12l-8.5 8.5a2.1 2.1 0 0 1-3-3L18 9" />
    <path d="M14.7 6.3 12 9" />
  </svg>
);

export const IconSend = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M4.4 12 20 4l-8 15.6-1.9-6.2z" />
    <path d="m10.1 13.4 9.9-9.4" />
  </svg>
);

export const IconStop = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none" />
  </svg>
);

export const IconClose = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);

export const IconSparkle = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path
      d="M12 3c.4 3.3 1.6 5 4.4 5.6.6.1.6 1 0 1.1C13.6 10.3 12.4 12 12 15.3c-.1.6-1 .6-1.1 0C10.5 12 9.3 10.3 6.5 9.7c-.6-.1-.6-1 0-1.1C9.3 8 10.5 6.3 10.9 3c.1-.6 1-.6 1.1 0Z"
      fill="currentColor"
      stroke="none"
    />
  </svg>
);

export const IconShield = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6z" />
  </svg>
);

export const IconAlert = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M12 4.5 21 19H3z" />
    <path d="M12 10v4M12 16.6v.1" />
  </svg>
);

export const IconFolder = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.2l2 2.5h7.8A2.5 2.5 0 0 1 21 10v6.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z" />
  </svg>
);

export const IconPulse = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M3 12h4l2.5-6 4 12L16 12h5" />
  </svg>
);

export const IconThumbUp = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M7 21V10l4.5-7a2 2 0 0 1 2.9 2.2L13.5 9H19a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 17.6 20H7z" />
    <path d="M7 10H4a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h3" />
  </svg>
);

export const IconThumbDown = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M17 3v11l-4.5 7a2 2 0 0 1-2.9-2.2l.9-3.8H5a2 2 0 0 1-2-2.4l1.4-7A2 2 0 0 1 6.4 4H17z" />
    <path d="M17 14h3a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1h-3" />
  </svg>
);

export const IconPin = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M15 3.5 20.5 9l-3 1-4 4-.7 3.6L8 13.2 4.4 12l3.6-.7 4-4z" />
    <path d="m8 13.2-4.5 7.3" />
  </svg>
);

export const IconPencil = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3z" />
    <path d="m14.5 6 3 3" />
  </svg>
);

export const IconKey = ({ size, className }: Props) => (
  <svg {...wrap(size, className)}>
    <circle cx="8" cy="15" r="4" />
    <path d="m11 12 8-8M17 6l2 2M14.5 8.5l2 2" />
  </svg>
);
