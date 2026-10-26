import { Tooltip } from 'antd'
import { stageLabel, statusMeta } from '../../theme'
import type { RecordingStatus } from '../../types'

interface StatusBadgeProps {
  status: RecordingStatus
  progress?: number
  stage?: string
  /** Show the current processing stage next to the label. */
  showStage?: boolean
  size?: 'default' | 'small'
  className?: string
}

export function StatusBadge({ status, progress, stage, showStage = false, size = 'default', className }: StatusBadgeProps) {
  const meta = statusMeta[status] ?? statusMeta.uploaded
  const isActive = status === 'queued' || status === 'processing' || status === 'uploaded'
  const percent = typeof progress === 'number' ? Math.max(0, Math.min(100, Math.round(progress))) : null

  const badge = (
    <span
      className={`status-badge ${size === 'small' ? 'status-badge--sm' : ''} ${className ?? ''}`}
      style={{ color: meta.color, background: meta.bg }}
    >
      <span className={`status-badge__dot ${isActive ? 'status-badge__dot--pulse' : ''}`} style={{ background: meta.dot }} />
      <span>{meta.label}</span>
      {isActive && percent !== null && <span className="status-badge__pct">{percent}%</span>}
    </span>
  )

  if (isActive && (showStage || stage)) {
    const stageText = stage ? stageLabel[stage] ?? stage : ''
    return <Tooltip title={stageText ? `当前阶段：${stageText}` : undefined}>{badge}</Tooltip>
  }
  return badge
}
