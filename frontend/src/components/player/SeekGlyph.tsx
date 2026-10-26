/** Circular "skip seconds" glyph used by the audio players (±15s). */
export function SeekGlyph({ forward = false }: { forward?: boolean }) {
  return (
    <span className={`seek-glyph ${forward ? 'seek-glyph--forward' : ''}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
        <path
          d="M12.4 4.4a7.8 7.8 0 1 1-7.2 5"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
        <path
          d="M4.6 4.6v4.6h4.6"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <text x="12.4" y="16.4" textAnchor="middle" fontSize="7.2" fontWeight="700" fill="currentColor">
          15
        </text>
      </svg>
    </span>
  )
}
