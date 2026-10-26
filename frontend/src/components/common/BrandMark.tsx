interface BrandMarkProps {
  size?: number
  /** Render on the colored gradient tile (default) or transparent. */
  plain?: boolean
  className?: string
}

const BARS = [
  { x: 6, h: 8 },
  { x: 10, h: 14 },
  { x: 14, h: 20 },
  { x: 18, h: 12 },
  { x: 22, h: 17 },
  { x: 26, h: 9 },
]

/** Echoic logo: a rounded gradient tile with a sound-wave glyph. */
export function BrandMark({ size = 36, plain = false, className }: BrandMarkProps) {
  return (
    <span
      className={`brand-mark ${plain ? 'brand-mark--plain' : ''} ${className ?? ''}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.3) }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 32 32" width={size} height={size} role="presentation">
        {BARS.map((bar) => (
          <rect
            key={bar.x}
            x={bar.x - 1.4}
            y={16 - bar.h / 2}
            width={2.8}
            height={bar.h}
            rx={1.4}
            fill={plain ? '#1677ff' : '#ffffff'}
            opacity={plain ? 0.9 : 0.96}
          />
        ))}
      </svg>
    </span>
  )
}
