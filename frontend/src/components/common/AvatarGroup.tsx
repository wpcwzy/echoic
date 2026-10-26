import { Avatar, Tooltip } from 'antd'
import { colorFromString, initials } from '../../utils/format'
import type { Speaker } from '../../types'

interface AvatarGroupProps {
  speakers: Speaker[]
  max?: number
  size?: number
  /** Render a leading "发言人" label pill like the reference player card. */
  label?: string
  className?: string
}

export function AvatarGroup({ speakers, max = 4, size = 26, label, className }: AvatarGroupProps) {
  const visible = speakers.slice(0, max)
  const rest = speakers.length - visible.length

  return (
    <div className={`avatar-group ${className ?? ''}`}>
      {label && <span className="avatar-group__label">{label}</span>}
      {visible.map((speaker, index) => {
        const name = speaker.display_name || speaker.label || '发言人'
        const color = speaker.color || colorFromString(name)
        return (
          <Tooltip key={speaker.id} title={name}>
            <Avatar
              size={size}
              className="avatar-group__item"
              style={{
                background: color,
                marginLeft: index === 0 ? 0 : -7,
                width: size,
                height: size,
                fontSize: Math.round(size * 0.4),
                zIndex: visible.length - index,
              }}
            >
              {initials(name, 1)}
            </Avatar>
          </Tooltip>
        )
      })}
      {rest > 0 && (
        <span
          className="avatar-group__more"
          style={{ width: size, height: size, marginLeft: -7, fontSize: Math.round(size * 0.38) }}
          title={speakers.slice(max).map((s) => s.display_name || s.label).join('、')}
        >
          +{rest}
        </span>
      )}
      {speakers.length === 0 && <span className="avatar-group__empty">未识别发言人</span>}
    </div>
  )
}
