import { Button, Progress, Tooltip } from 'antd'
import {
  CalendarOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  PlayCircleFilled,
  RobotOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { RecordingStatus, RecordingSummary } from '../../types'
import { statusMeta } from '../../theme'
import { formatDateShort, formatMs, formatRelative, hexToRgba } from '../../utils/format'
import { StatusBadge } from '../common/StatusBadge'
import { AvatarGroup } from '../common/AvatarGroup'

interface RecordingCardProps {
  recording: RecordingSummary
}

const ACTIVE_STATUS: RecordingStatus[] = ['uploaded', 'queued', 'processing']

function TagChips({ recording }: { recording: RecordingSummary }) {
  const tags = recording.tags?.slice(0, 2) ?? []
  if (!tags.length) return null
  return (
    <span className="tag-chips">
      {tags.map((tag) => (
        <span
          key={tag.id}
          className="tag-chip"
          style={{ color: tag.color || '#1677ff', background: hexToRgba(tag.color || '#1677ff', 0.1) }}
        >
          {tag.name}
        </span>
      ))}
    </span>
  )
}

/** Library grid card with hover play affordance. */
export function RecordingCard({ recording }: RecordingCardProps) {
  const navigate = useNavigate()
  const active = ACTIVE_STATUS.includes(recording.status)
  const failed = recording.status === 'failed'
  const open = () => navigate(`/r/${recording.id}`)

  return (
    <article
      className={`recording-card recording-card--${recording.status}`}
      role="button"
      tabIndex={0}
      aria-label={`打开录音 ${recording.title}`}
      onClick={open}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          open()
        }
      }}
    >
      <div className="recording-card__top">
        <StatusBadge status={recording.status} progress={recording.progress} stage={recording.stage} size="small" />
        <Tooltip title="录音时间">
          <span className="recording-card__date">{formatRelative(recording.recorded_at || recording.created_at)}</span>
        </Tooltip>
      </div>

      <h3 className="recording-card__title" title={recording.title}>
        {recording.title || '未命名录音'}
      </h3>
      <p className="recording-card__desc">{recording.description || '暂无描述'}</p>

      {active && (
        <div className="recording-card__progress">
          <Progress
            percent={Math.round(recording.progress || 0)}
            size="small"
            showInfo={false}
            strokeColor={{ from: '#69b1ff', to: '#1677ff' }}
            trailColor="rgba(31,35,41,0.06)"
          />
        </div>
      )}
      {failed && <div className="recording-card__error">{recording.error_message || '处理失败，可进入详情重试'}</div>}

      <div className="recording-card__meta">
        <span>
          <ClockCircleOutlined /> {formatMs(recording.duration_ms)}
        </span>
        <Tooltip title="录音时间">
          <span>
            <CalendarOutlined /> {formatDateShort(recording.recorded_at || recording.created_at)}
          </span>
        </Tooltip>
        <TagChips recording={recording} />
      </div>

      <footer className="recording-card__footer">
        <AvatarGroup speakers={recording.speakers} max={3} size={22} />
        <div className="recording-card__footer-right">
          {recording.has_transcript && (
            <Tooltip title="已有转录">
              <FileTextOutlined className="recording-card__ai-icon" />
            </Tooltip>
          )}
          {recording.insight_kinds?.length > 0 && (
            <Tooltip title={`已有 ${recording.insight_kinds.length} 项 AI 分析`}>
              <RobotOutlined className="recording-card__ai-icon" />
            </Tooltip>
          )}
          <Tooltip title="打开并播放">
            <button
              type="button"
              className="recording-card__play"
              aria-label="打开并播放"
              onClick={(event) => {
                event.stopPropagation()
                navigate(`/r/${recording.id}?autoplay=1`)
              }}
            >
              <PlayCircleFilled />
            </button>
          </Tooltip>
        </div>
      </footer>
    </article>
  )
}

/** Compact list row for the list view. */
export function RecordingRow({ recording }: RecordingCardProps) {
  const navigate = useNavigate()
  const meta = statusMeta[recording.status] ?? statusMeta.uploaded
  const open = () => navigate(`/r/${recording.id}`)

  return (
    <div
      className="recording-row"
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          open()
        }
      }}
    >
      <div className="recording-row__icon" style={{ color: meta.color, background: meta.bg }}>
        {recording.media_type === 'video' ? <FileTextOutlined /> : <FileTextOutlined />}
      </div>
      <div className="recording-row__main">
        <div className="recording-row__title-line">
          <span className="recording-row__title">{recording.title || '未命名录音'}</span>
          <TagChips recording={recording} />
        </div>
        <div className="recording-row__meta">
          <span>{formatDateShort(recording.created_at)}</span>
          <span>·</span>
          <span>
            <TeamOutlined /> {recording.speakers?.length ?? 0} 位发言人
          </span>
          {recording.description && (
            <>
              <span>·</span>
              <span className="recording-row__desc">{recording.description}</span>
            </>
          )}
        </div>
      </div>
      <StatusBadge status={recording.status} progress={recording.progress} stage={recording.stage} size="small" />
      <span className="recording-row__duration">{formatMs(recording.duration_ms)}</span>
      <Tooltip title="打开">
        <Button
          type="text"
          shape="circle"
          icon={<PlayCircleFilled />}
          aria-label="打开并播放"
          onClick={(event) => {
            event.stopPropagation()
            navigate(`/r/${recording.id}?autoplay=1`)
          }}
        />
      </Tooltip>
    </div>
  )
}
