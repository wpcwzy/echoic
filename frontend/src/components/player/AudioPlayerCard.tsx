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

interface AudioPlayerCardProps {
  recording: RecordingDetail
  speakers: Speaker[]
  chapters: Chapter[]
}

function SeekGlyph({ forward = false }: { forward?: boolean }) {
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

/**
 * Feishu-Miaoji style player card: custom seekable progress bar (with chapter
 * ticks), speaker avatars, ±15s / play, playback rate and more menu.
 */
export function AudioPlayerCard({ recording, speakers, chapters }: AudioPlayerCardProps) {
  const { currentMs, durationMs, playing, rate, hasMedia, toggle, seekBy, seekTo, setRate } = useAudioPlayer()
  const trackRef = useRef<HTMLDivElement | null>(null)
  const [dragMs, setDragMs] = useState<number | null>(null)

  const totalMs = durationMs || recording.duration_ms || 0
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

  const exportItems: MenuProps['items'] = [
    { key: 'txt', label: <a href={exportUrl(recording.id, 'txt')}>TXT 文本</a> },
    { key: 'srt', label: <a href={exportUrl(recording.id, 'srt')}>SRT 字幕</a> },
    { key: 'md', label: <a href={exportUrl(recording.id, 'md')}>Markdown</a> },
    { key: 'json', label: <a href={exportUrl(recording.id, 'json')}>JSON 数据</a> },
  ]

  const moreItems: MenuProps['items'] = [
    {
      key: 'download',
      icon: <DownloadOutlined />,
      label: <a href={mediaUrl(recording.id, true)}>下载原文件</a>,
    },
    {
      key: 'export',
      icon: <ExportOutlined />,
      label: '导出转录稿',
      children: recording.has_transcript || recording.status === 'ready' ? exportItems : [{ key: 'none', label: '转录尚未生成', disabled: true }],
    },
  ]

  return (
    <section className={`player-card ${disabled ? 'player-card--no-media' : ''}`} aria-label="录音播放器">
      <div className="player-card__top">
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

      <div className="player-card__bottom">
        <div className="player-card__speakers">
          <AvatarGroup speakers={speakers} max={4} size={27} label="发言人" />
        </div>

        <div className="player-controls">
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

        <div className="player-card__right">
          <Dropdown menu={{ items: rateItems, onClick: ({ key }) => setRate(Number(key)) }} trigger={['click']} placement="bottomRight">
            <button type="button" className="player-rate" aria-label="播放速度">
              {rate === 1 ? '1.0' : rate} x
            </button>
          </Dropdown>
          <Dropdown menu={{ items: moreItems }} trigger={['click']} placement="bottomRight">
            <button type="button" className="player-more" aria-label="更多操作">
              <MoreOutlined />
            </button>
          </Dropdown>
        </div>
      </div>

      {disabled && (
        <div className="player-card__notice">该录音暂无可用媒体文件（演示数据），播放控制已禁用</div>
      )}
    </section>
  )
}
