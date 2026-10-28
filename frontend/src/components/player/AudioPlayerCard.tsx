import { useAudioPlayer } from '../../hooks/useAudioPlayer'
import type { RecordingDetail } from '../../types'
import { VideoSceneStrip } from './VideoSceneStrip'

interface AudioPlayerCardProps {
  recording: RecordingDetail
}

/**
 * Media stage for the recording detail page (v1.0.3).
 *
 * Video recordings keep a sticky picture at the top of the content column plus
 * the scene filmstrip; audio recordings have no stage because the always-visible
 * playback controls now live in the bottom :component:`PlayerControlsBar`.
 */
export function AudioPlayerCard({ recording }: AudioPlayerCardProps) {
  const { isVideo, hasMedia, attachMediaElement, mediaUrl: mediaSrc } = useAudioPlayer()

  if (!isVideo || !hasMedia) return null

  return (
    <>
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
      {recording.id > 0 && <VideoSceneStrip recordingId={recording.id} />}
    </>
  )
}
