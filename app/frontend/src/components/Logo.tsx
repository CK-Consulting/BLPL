/**
 * The BLPL mark: a square split into quadrants, BL above, PL below.
 *
 * Inline SVG rather than an image file, for two reasons that both matter on the
 * screens it appears on. It inherits `currentColor`, so the same component works
 * on the dark app chrome and on a card without a second asset; and it needs no
 * network request, so it is present on the very first paint of the unlock screen
 * — which is exactly the screen where not knowing what app you are looking at is
 * the problem.
 */

export function Logo({ size = 32, withText = false }: { size?: number; withText?: boolean }) {
  const mark = (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="BLPL"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="1.5" y="1.5" width="61" height="61" rx="10" stroke="currentColor" strokeWidth="3" />
      {/* The quad divisions. Thinner than the frame so the letters stay the
          loudest thing in the mark at small sizes. */}
      <path d="M32 3 V61 M3 32 H61" stroke="currentColor" strokeWidth="2" opacity="0.55" />
      <g
        fill="currentColor"
        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        fontSize="24"
        fontWeight="700"
        textAnchor="middle"
        dominantBaseline="central"
      >
        <text x="17" y="18">B</text>
        <text x="47" y="18">L</text>
        <text x="17" y="47">P</text>
        <text x="47" y="47">L</text>
      </g>
    </svg>
  );

  if (!withText) return mark;
  return (
    <div className="logo-lockup">
      {mark}
      <span className="logo-word">BLPL</span>
    </div>
  );
}
