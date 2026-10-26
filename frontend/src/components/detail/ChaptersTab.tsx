import { useMemo } from 'react'
import { Button } from 'antd'
import { ClockCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import type { Chapter, Insights } from '../../types'
import { formatMs } from '../../utils/format'
import { EmptyState } from '../common/EmptyState'
import { TabContentSkeleton } from '../common/Skeletons'
import { ErrorState } from '../common/ErrorState'

interface ChaptersTabProps {
  insights: Insights | undefined
  loading: boolean
  error: unknown
  currentMs: number
  playing: boolean
  regenerating: boolean
  onSeek: (ms: number) => void
  onRegenerate: () => void
  onRetry: () => void
}

export function ChaptersTab({
  insights,
  loading,
  error,
  currentMs,
  playing,
  regenerating,
  onSeek,
  onRegenerate,
  onRetry,
}: ChaptersTabProps) {
  const chapters: Chapter[] = insights?.chapters ?? []
  const activeId = useMemo(() => {
    if (!playing) return null
    return chapters.find((chapter) => currentMs >= chapter.start_ms && currentMs < chapter.end_ms)?.id ?? null
  }, [chapters, currentMs, playing])

  if (loading) return <TabContentSkeleton rows={3} />
  if (error) return <ErrorState title="章节加载失败" description="请稍后重试。" onRetry={onRetry} />
  if (!chapters.length) {
    return (
      <EmptyState
        icon={<ClockCircleOutlined />}
        title="章节尚未生成"
        description="AI 会根据讨论内容自动划分章节，方便快速跳转到感兴趣的部分。"
        actions={
          <Button type="primary" ghost loading={regenerating} icon={<ReloadOutlined />} onClick={onRegenerate}>
            生成章节
          </Button>
        }
      />
    )
  }

  return (
    <div className="chapters-tab">
      <div className="chapters-list">
        {chapters.map((chapter, index) => {
          const active = chapter.id === activeId
          return (
            <button
              key={chapter.id}
              type="button"
              className={`chapter-row ${active ? 'is-active' : ''}`}
              onClick={() => onSeek(chapter.start_ms)}
            >
              <div className="chapter-row__rail">
                <span className="chapter-row__dot" />
                {index < chapters.length - 1 && <span className="chapter-row__line" />}
              </div>
              <div className="chapter-row__body">
                <div className="chapter-row__head">
                  <span className="chapter-row__time">
                    {formatMs(chapter.start_ms)} – {formatMs(chapter.end_ms)}
                  </span>
                  <span className="chapter-row__title">{chapter.title}</span>
                  {active && <span className="chapter-row__playing">正在播放</span>}
                </div>
                {chapter.summary && <p className="chapter-row__summary">{chapter.summary}</p>}
                {chapter.bullets?.length ? (
                  <ul className="chapter-row__bullets">
                    {chapter.bullets.map((bullet, bulletIndex) => (
                      <li key={bulletIndex}>{bullet}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
