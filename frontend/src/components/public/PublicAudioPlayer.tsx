import { useAudioPlayer } from '../../hooks/useAudioPlayer'

/**
 * Public share page media stage (v1.0.3): video shares keep the sticky picture,
 * while playback controls are rendered by the fixed :component:`PlayerControlsBar`.
 */
export function PublicAudioPlayer() {
  const { isVideo, hasMedia, attachMediaElement, mediaUrl: mediaSrc } = useAudioPlayer()

  if (!isVideo || !hasMedia) return null

  return (
    <div className="player-video-dock">
      <div className="player-video">
        <video
          ref={attachMediaElement}
          className="player-video__element"
          src={mediaSrc}
          controls
          playsInline
          preload="metadata"
        />
      </div>
    </div>
  )
}
