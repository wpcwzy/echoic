import { useRef, useState } from 'react'
import { Dropdown, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import {
  CaretRightOutlined,
  DownloadOutlined,
  ExportOutlined,
  MoreOutlined,
  PauseOutlined,
} from '@ant-design/icons'
import { useAudioPlayer } from '../../hooks/useAudioPlayer'
import { formatMs } from '../../utils/format'
import { exportUrl, mediaUrl } from '../../api/queries'
import type { Chapter, RecordingDetail, Speaker } from '../../types'
import { AvatarGroup } from '../common/AvatarGroup'
import { SeekGlyph } from './SeekGlyph'

interface PlayerControlsBarProps {
  /** ``detail`` shows title/speakers + export & download menu; ``public`` only the share download. */
  variant: 'detail' | 'public'
  chapters: Chapter[]
  /** Optional bar title (recording title); hidden on mobile. */
  title?: string
  speakers?: Speaker[]
  recording?: RecordingDetail
  /** Public share: link to the original media file (only when the share allows download). */
  publicDownloadHref?: string
  /** Public share: whether downloading is allowed at all. */
  allowDownload?: boolean
  className?: string
}

/**
 * Always-reachable playback controls (v1.0.3).
 *
 * On the recording detail page the bar is the last flex child of the content
 * column, so it stays pinned above the AI panel; on mobile and on the public
 * share page it becomes a viewport-fixed dock above the mobile navigation.
 */
export function PlayerControlsBar({
  variant,
  chapters,
  title,
  speakers = [],
  recording,
  publicDownloadHref,
  allowDownload = false,
  className,
}: PlayerControlsBarProps) {
  const { currentMs, durationMs, playing, rate, hasMedia, isVideo, toggle, seekBy, seekTo, setRate } = useAudioPlayer()
  const trackRef = useRef<HTMLDivElement | null>(null)
  const [dragMs, setDragMs] = useState<number | null>(null)

  const totalMs = durationMs || recording?.duration_ms || 0
  const displayMs = dragMs ?? currentMs
  const percent = totalMs > 0 ? Math.min(100, Math.max(0, (displayMs / totalMs) * 100)) : 0
  const disabled = !hasMedia

  const msFromClientX = (clientX: number): number => {
    const track = trackRef.current
    if (!track || totalMs <= 0) return 0
    const rect = track.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    return ratio * totalMs
  }

  const rateItems: MenuProps['items'] = [0.75, 1, 1.25, 1.5, 2].map((value) => ({
    key: String(value),
    label: (
      <span className={`player-menu__rate ${value === rate ? 'is-active' : ''}`}>
        {value === 1 ? '1.0' : value} x {value === rate && <span className="player-menu__check">✓</span>}
      </span>
    ),
  }))

  const exportItems: MenuProps['items'] = recording
    ? [
        { key: 'txt', label: <a href={exportUrl(recording.id, 'txt')}>TXT 文本</a> },
        { key: 'srt', label: <a href={exportUrl(recording.id, 'srt')}>SRT 字幕</a> },
        { key: 'md', label: <a href={exportUrl(recording.id, 'md')}>Markdown</a> },
        { key: 'json', label: <a href={exportUrl(recording.id, 'json')}>JSON 数据</a> },
      ]
    : []

  const moreItems: MenuProps['items'] = recording
    ? [
        {
          key: 'download',
          icon: <DownloadOutlined />,
          label: <a href={mediaUrl(recording.id, true)}>下载原文件</a>,
        },
        {
          key: 'export',
          icon: <ExportOutlined />,
          label: '导出转录稿',
          children:
            recording.has_transcript || recording.status === 'ready'
              ? exportItems
              : [{ key: 'none', label: '转录尚未生成', disabled: true }],
        },
      ]
    : []

  return (
    <div
      className={`player-bar player-bar--${variant} ${disabled ? 'player-bar--no-media' : ''} ${className ?? ''}`}
      aria-label={isVideo ? '视频播放控制' : '录音播放控制'}
    >
      <div className="player-bar__inner">
        {(title || speakers.length > 0) && (
          <div className="player-bar__meta">
            {title && (
              <span className="player-bar__title" title={title}>
                {title}
              </span>
            )}
            {speakers.length > 0 && <AvatarGroup speakers={speakers} max={3} size={22} label="发言人" />}
          </div>
        )}

        <div className="player-bar__seek">
            <Tooltip title="后退 15 秒">
              <button
                type="button"
                className="player-control player-control--seek"
                aria-label="后退 15 秒"
                disabled={disabled}
                onClick={() => seekBy(-15000)}
              >
                <SeekGlyph />
              </button>
            </Tooltip>
            <Tooltip title={disabled ? '暂无媒体文件' : playing ? '暂停' : '播放'}>
              <button
                type="button"
                className="player-control player-control--play"
                aria-label={playing ? '暂停' : '播放'}
                disabled={disabled}
                onClick={toggle}
              >
                {playing ? <PauseOutlined /> : <CaretRightOutlined />}
              </button>
            </Tooltip>
            <Tooltip title="前进 15 秒">
              <button
                type="button"
                className="player-control player-control--seek"
                aria-label="前进 15 秒"
                disabled={disabled}
                onClick={() => seekBy(15000)}
              >
                <SeekGlyph forward />
              </button>
            </Tooltip>
          </div>

        <div className="player-bar__timeline">
          <span className="player-card__time">{formatMs(displayMs)}</span>
            <div
              ref={trackRef}
              className="player-progress"
              role="slider"
              tabIndex={0}
              aria-label="播放进度"
              aria-valuemin={0}
              aria-valuemax={Math.round(totalMs / 1000)}
              aria-valuenow={Math.round(displayMs / 1000)}
              onPointerDown={(event) => {
                if (disabled) return
                event.currentTarget.setPointerCapture(event.pointerId)
                setDragMs(msFromClientX(event.clientX))
              }}
              onPointerMove={(event) => {
                if (dragMs === null) return
                setDragMs(msFromClientX(event.clientX))
              }}
              onPointerUp={(event) => {
                if (dragMs === null) return
                const target = msFromClientX(event.clientX)
                setDragMs(null)
                seekTo(target)
              }}
              onPointerCancel={() => setDragMs(null)}
              onKeyDown={(event) => {
                if (disabled) return
                if (event.key === 'ArrowRight') {
                  event.preventDefault()
                  seekBy(5000)
                }
                if (event.key === 'ArrowLeft') {
                  event.preventDefault()
                  seekBy(-5000)
                }
                if (event.key === ' ' || event.key === 'Enter') {
                  event.preventDefault()
                  toggle()
                }
              }}
            >
              <div className="player-progress__track">
                <div className="player-progress__fill" style={{ width: `${percent}%` }} />
                {totalMs > 0 &&
                  chapters
                    .filter((chapter) => chapter.start_ms > 0 && chapter.start_ms < totalMs)
                    .map((chapter) => (
                      <span
                        key={chapter.id}
                        className="player-progress__tick"
                        style={{ left: `${(chapter.start_ms / totalMs) * 100}%` }}
                      />
                    ))}
                <span className={`player-progress__knob ${dragMs !== null ? 'is-dragging' : ''}`} style={{ left: `${percent}%` }} />
              </div>
            </div>
            <span className="player-card__time">{formatMs(totalMs)}</span>
        </div>

        <div className="player-bar__right">
          <Dropdown menu={{ items: rateItems, onClick: ({ key }) => setRate(Number(key)) }} trigger={['click']} placement="topRight">
            <button type="button" className="player-rate" aria-label="播放速度">
              {rate === 1 ? '1.0' : rate} x
            </button>
          </Dropdown>
          {variant === 'detail' && recording && (
            <Dropdown menu={{ items: moreItems }} trigger={['click']} placement="topRight">
              <button type="button" className="player-more" aria-label="更多操作">
                <MoreOutlined />
              </button>
            </Dropdown>
          )}
          {variant === 'public' && allowDownload && publicDownloadHref && (
            <Tooltip title="下载原文件">
              <a className="player-more public-player__download" href={publicDownloadHref} aria-label="下载原文件">
                <DownloadOutlined />
              </a>
            </Tooltip>
          )}
        </div>
      </div>

      {disabled && (
        <div className="player-card__notice">
          {variant === 'public' ? '媒体文件加载失败，分享链接可能已失效或文件已删除' : '该录音暂无可用媒体文件，播放控制已禁用'}
        </div>
      )}
    </div>
  )
}