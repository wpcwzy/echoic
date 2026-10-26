import { Button, Checkbox, Progress, Tooltip } from 'antd'
import {
  CalendarOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  FileTextOutlined,
  PlayCircleFilled,
  RobotOutlined,
  TeamOutlined,
  UndoOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import type { RecordingStatus, RecordingSummary } from '../../types'
import { statusMeta } from '../../theme'
import { daysUntilPurge, formatDateShort, formatMs, formatRelative, hexToRgba } from '../../utils/format'
import { StatusBadge } from '../common/StatusBadge'
import { AvatarGroup } from '../common/AvatarGroup'

export interface RecordingCardProps {
  recording: RecordingSummary
  /** Admin view: show which user owns the recording. */
  showOwner?: boolean
  /** v0.3: show a selection checkbox (library multi-select). */
  selectable?: boolean
  selected?: boolean
  onSelectToggle?: (id: number, checked: boolean) => void
  /** v0.3 trash view: read-only card with restore / purge actions. */
  trashMode?: boolean
  retentionDays?: number
  onRestore?: (recording: RecordingSummary) => void
  onPurge?: (recording: RecordingSummary) => void
  restoring?: boolean
  purging?: boolean
}

function OwnerChip({ recording }: { recording: RecordingSummary }) {
  if (!recording.owner) return null
  const name = recording.owner.display_name || recording.owner.username
  return (
    <Tooltip title={`归属用户：${name}（@${recording.owner.username}）`}>
      <span className="owner-chip">
        <UserOutlined /> {name}
      </span>
    </Tooltip>
  )
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
          style={{ color: tag.color || 'var(--brand)', background: hexToRgba(tag.color || '#1677ff', 0.1) }}
        >
          {tag.name}
        </span>
      ))}
    </span>
  )
}

function TrashBadge({ recording, retentionDays = 30 }: { recording: RecordingSummary; retentionDays?: number }) {
  const days = daysUntilPurge(recording.deleted_at, retentionDays)
  return (
    <span className="trash-badge">
      <DeleteOutlined /> 剩余 {days} 天
    </span>
  )
}

function SelectionCheckbox({
  recording,
  selectable,
  selected,
  onSelectToggle,
}: Pick<RecordingCardProps, 'recording' | 'selectable' | 'selected' | 'onSelectToggle'>) {
  if (!selectable) return null
  return (
    <Checkbox
      className="recording-select"
      checked={selected}
      aria-label={`选择 ${recording.title || '录音'}`}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => {
        event.stopPropagation()
        onSelectToggle?.(recording.id, event.target.checked)
      }}
    />
  )
}

/** Library grid card with optional selection + trash-mode actions. */
export function RecordingCard({
  recording,
  showOwner = false,
  selectable = false,
  selected = false,
  onSelectToggle,
  trashMode = false,
  retentionDays = 30,
  onRestore,
  onPurge,
  restoring = false,
  purging = false,
}: RecordingCardProps) {
  const navigate = useNavigate()
  const active = ACTIVE_STATUS.includes(recording.status) && !trashMode
  const failed = recording.status === 'failed' && !trashMode
  const open = () => {
    if (!trashMode) navigate(`/r/${recording.id}`)
  }

  return (
    <article
      className={`recording-card recording-card--${recording.status} ${trashMode ? 'recording-card--trashed' : ''} ${
        selected ? 'is-selected' : ''
      }`}
      role={trashMode ? undefined : 'button'}
      tabIndex={trashMode ? undefined : 0}
      aria-label={trashMode ? undefined : `打开录音 ${recording.title}`}
      onClick={open}
      onKeyDown={(event) => {
        if (trashMode) return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          open()
        }
      }}
    >
      <div className="recording-card__top">
        <div className="recording-card__top-left">
          <SelectionCheckbox
            recording={recording}
            selectable={selectable}
            selected={selected}
            onSelectToggle={onSelectToggle}
          />
          {trashMode ? <TrashBadge recording={recording} retentionDays={retentionDays} /> : (
            <StatusBadge status={recording.status} progress={recording.progress} stage={recording.stage} size="small" />
          )}
        </div>
        <span className="recording-card__date">
          {trashMode && recording.deleted_at
            ? `删除于 ${formatDateShort(recording.deleted_at)}`
            : formatRelative(recording.recorded_at || recording.created_at)}
        </span>
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
            trailColor="var(--fill-1)"
          />
        </div>
      )}
      {failed && <div className="recording-card__error">{recording.error_message || '处理失败，可进入详情重试'}</div>}

      <div className="recording-card__meta">
        <span>
          <ClockCircleOutlined /> {formatMs(recording.duration_ms)}
        </span>
        <span>
          <CalendarOutlined /> {formatDateShort(recording.recorded_at || recording.created_at)}
        </span>
        {showOwner && <OwnerChip recording={recording} />}
        <TagChips recording={recording} />
      </div>

      <footer className="recording-card__footer">
        {trashMode ? (
          <>
            <span className="recording-card__trash-hint">
              {retentionDays} 天后自动彻底删除
            </span>
            <div className="recording-card__trash-actions">
              <Button size="small" icon={<UndoOutlined />} loading={restoring} onClick={() => onRestore?.(recording)}>
                恢复
              </Button>
              <Button size="small" danger icon={<DeleteOutlined />} loading={purging} onClick={() => onPurge?.(recording)}>
                彻底删除
              </Button>
            </div>
          </>
        ) : (
          <>
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
          </>
        )}
      </footer>
    </article>
  )
}

/** Compact list row for the list view. */
export function RecordingRow({
  recording,
  showOwner = false,
  selectable = false,
  selected = false,
  onSelectToggle,
  trashMode = false,
  retentionDays = 30,
  onRestore,
  onPurge,
  restoring = false,
  purging = false,
}: RecordingCardProps) {
  const navigate = useNavigate()
  const safeStatus = statusMeta[recording.status] ? recording.status : 'uploaded'
  const open = () => {
    if (!trashMode) navigate(`/r/${recording.id}`)
  }

  return (
    <div
      className={`recording-row ${trashMode ? 'recording-row--trashed' : ''} ${selected ? 'is-selected' : ''}`}
      role={trashMode ? undefined : 'button'}
      tabIndex={trashMode ? undefined : 0}
      onClick={open}
      onKeyDown={(event) => {
        if (trashMode) return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          open()
        }
      }}
    >
      {selectable && (
        <Checkbox
          className="recording-select"
          checked={selected}
          aria-label={`选择 ${recording.title || '录音'}`}
          onClick={(event) => event.stopPropagation()}
          onChange={(event) => {
            event.stopPropagation()
            onSelectToggle?.(recording.id, event.target.checked)
          }}
        />
      )}
      <div
        className="recording-row__icon"
        style={{ color: `var(--status-${safeStatus}-fg)`, background: `var(--status-${safeStatus}-bg)` }}
      >
        <FileTextOutlined />
      </div>
      <div className="recording-row__main">
        <div className="recording-row__title-line">
          <span className="recording-row__title">{recording.title || '未命名录音'}</span>
          <TagChips recording={recording} />
        </div>
        <div className="recording-row__meta">
          <span>{formatDateShort(recording.recorded_at || recording.created_at)}</span>
          <span>·</span>
          <span>
            <TeamOutlined /> {recording.speakers?.length ?? 0} 位发言人
          </span>
          {showOwner && recording.owner && (
            <>
              <span>·</span>
              <OwnerChip recording={recording} />
            </>
          )}
          {trashMode && recording.deleted_at && (
            <>
              <span>·</span>
              <span className="recording-row__deleted">删除于 {formatDateShort(recording.deleted_at)}</span>
            </>
          )}
          {recording.description && (
            <>
              <span>·</span>
              <span className="recording-row__desc">{recording.description}</span>
            </>
          )}
        </div>
      </div>
      {trashMode ? <TrashBadge recording={recording} retentionDays={retentionDays} /> : (
        <StatusBadge status={recording.status} progress={recording.progress} stage={recording.stage} size="small" />
      )}
      <span className="recording-row__duration">{formatMs(recording.duration_ms)}</span>
      {trashMode ? (
        <div className="recording-row__trash-actions">
          <Tooltip title="恢复到资料库">
            <Button
              type="text"
              size="small"
              icon={<UndoOutlined />}
              aria-label="恢复录音"
              loading={restoring}
              onClick={(event) => {
                event.stopPropagation()
                onRestore?.(recording)
              }}
            />
          </Tooltip>
          <Tooltip title="彻底删除（无法恢复）">
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              aria-label="彻底删除"
              loading={purging}
              onClick={(event) => {
                event.stopPropagation()
                onPurge?.(recording)
              }}
            />
          </Tooltip>
        </div>
      ) : (
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
      )}
    </div>
  )
}
