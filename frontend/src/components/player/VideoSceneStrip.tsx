import { useEffect, useMemo, useRef } from 'react'
import { PictureOutlined } from '@ant-design/icons'
import { useAudioPlayer } from '../../hooks/useAudioPlayer'
import { sceneImageUrl, useRecordingScenes } from '../../api/scenes'
import { formatMs } from '../../utils/format'

interface VideoSceneStripProps {
  recordingId: number
}

const SKELETON_COUNT = 5

/**
 * Horizontally scrollable filmstrip of generated video scene thumbnails.
 * Clicking a thumbnail seeks the shared player cursor; the scene currently
 * being played is highlighted and scrolled into view.
 */
export function VideoSceneStrip({ recordingId }: VideoSceneStripProps) {
  const { currentMs, seekTo } = useAudioPlayer()
  const scenesQuery = useRecordingScenes(recordingId)
  const trackRef = useRef<HTMLDivElement | null>(null)
  const itemRefs = useRef<Record<number, HTMLButtonElement | null>>({})
  const scenes = scenesQuery.data?.items ?? []

  // Nearest preceding scene (scenes are ordered by time).
  const activeSceneId = useMemo(() => {
    let active: number | null = null
    for (const scene of scenes) {
      if (scene.time_ms <= currentMs + 120) active = scene.id
      else break
    }
    return active
  }, [currentMs, scenes])

  // Keep the active thumbnail centred in the strip while playing.
  useEffect(() => {
    if (activeSceneId === null) return
    const track = trackRef.current
    const item = itemRefs.current[activeSceneId]
    if (!track || !item) return
    const left = item.offsetLeft
    const right = left + item.offsetWidth
    const visibleLeft = track.scrollLeft
    const visibleRight = visibleLeft + track.clientWidth
    if (left < visibleLeft || right > visibleRight) {
      track.scrollTo({
        left: Math.max(0, left - track.clientWidth / 2 + item.offsetWidth / 2),
        behavior: 'smooth',
      })
    }
  }, [activeSceneId])

  // Keep the player card clean: hide the strip when there is nothing to show.
  if (!scenesQuery.isLoading && (scenesQuery.isError || scenes.length === 0)) return null

  return (
    <div className="scene-strip" aria-label="视频画面片段">
      <div className="scene-strip__head">
        <span className="scene-strip__title">
          <PictureOutlined /> 画面片段
        </span>
        <span className="scene-strip__meta">
          {scenesQuery.isLoading ? '加载画面中…' : `${scenes.length} 个片段 · 点击跳转`}
        </span>
      </div>
      <div className="scene-strip__track" ref={trackRef}>
        {scenesQuery.isLoading
          ? Array.from({ length: SKELETON_COUNT }).map((_, index) => (
              <div className="scene-thumb scene-thumb--skeleton" key={`scene-skeleton-${index}`} aria-hidden="true" />
            ))
          : scenes.map((scene) => {
              const active = scene.id === activeSceneId
              return (
                <button
                  key={scene.id}
                  ref={(element) => {
                    itemRefs.current[scene.id] = element
                  }}
                  type="button"
                  className={`scene-thumb ${active ? 'is-active' : ''}`}
                  onClick={() => seekTo(scene.time_ms)}
                  aria-label={`跳转到 ${formatMs(scene.time_ms)} 的画面`}
                  aria-current={active ? 'true' : undefined}
                  title={`跳转到 ${formatMs(scene.time_ms)}`}
                >
                  <img
                    className="scene-thumb__img"
                    src={sceneImageUrl(recordingId, scene.id)}
                    alt={`${formatMs(scene.time_ms)} 画面`}
                    loading="lazy"
                    draggable={false}
                    onError={(event) => {
                      event.currentTarget.classList.add('is-broken')
                    }}
                  />
                  <span className="scene-thumb__time">{formatMs(scene.time_ms)}</span>
                </button>
              )
            })}
      </div>
    </div>
  )
}
