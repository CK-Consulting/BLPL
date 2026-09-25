/**
 * Inline SVG icons for the navigation bar.
 *
 * Inline rather than a font or a sprite so an icon cannot arrive after the
 * text it replaces — a navbar that reflows once the webfont lands is the
 * jitter this was meant to remove.
 *
 * Every icon is `aria-hidden`. The accessible name belongs to the control, not
 * the drawing: a button that says "Project Settings" is understandable whether
 * or not its icon renders, and an icon that announces itself alongside a label
 * says everything twice.
 */

type IconProps = { size?: number; className?: string };

function svg(path: React.ReactNode, { size = 16, className = "" }: IconProps) {
  return (
    <svg
      className={`icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  );
}

/**
 * Crossed wrench and screwdriver — project settings.
 *
 * Distinct from the cog on purpose. A cog is the universal mark for "the
 * application's own preferences", and using it for both meant the only thing
 * separating *this project's* settings from *your account's* was position in a
 * crowded bar.
 */
export const WrenchScrewdriver = (p: IconProps = {}) =>
  svg(
    <>
      <path d="M14.7 6.3a4 4 0 0 0 5 5L21 12l-9 9-3-3 9-9 1.7-2.7Z" />
      <path d="M6 3 3 6l4.5 4.5" />
      <path d="m3 21 7-7" />
      <path d="M7.5 10.5 10 8" />
    </>,
    p,
  );

/** Cog — the account's and the application's own settings. */
export const Cog = (p: IconProps = {}) =>
  svg(
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
    </>,
    p,
  );

/** Plus in a square — start or clone a project. */
export const PlusSquare = (p: IconProps = {}) =>
  svg(
    <>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M12 8v8M8 12h8" />
    </>,
    p,
  );

/** Speech bubble — the chat panel. */
export const Chat = (p: IconProps = {}) =>
  svg(<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.4 8.4 0 0 1-3.8-.9L3 21l2-4.1A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5Z" />, p);

/** Share — invite someone to this project. */
export const Share = (p: IconProps = {}) =>
  svg(
    <>
      <circle cx="18" cy="5" r="3" />
      <circle cx="6" cy="12" r="3" />
      <circle cx="18" cy="19" r="3" />
      <path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4" />
    </>,
    p,
  );

/** Open in a new tab. */
export const ExternalLink = (p: IconProps = {}) =>
  svg(
    <>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <path d="M15 3h6v6M10 14 21 3" />
    </>,
    p,
  );

/** Overflow — the rest of the bar, when the bar has run out of room. */
export const Ellipsis = (p: IconProps = {}) =>
  svg(
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>,
    p,
  );
