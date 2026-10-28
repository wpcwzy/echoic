import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { toast } from '../utils/toast'
import { mediaUrl } from '../api/queries'

interface AudioPlayerContextValue {
  mediaUrl: string
  /** True for recordings whose editable media is a video file. */
  isVideo: boolean
  /** Attach the <video> element rendered by the player card (video recordings only). */
  attachMediaElement: (element: HTMLMediaElement | null) => void
  /** Media metadata has loaded and playback is possible. */
  ready: boolean
  /** False when the media request failed (e.g. demo metadata without a file). */
  hasMedia: boolean
  playing: boolean
  currentMs: number
  durationMs: number
  rate: number
  seekTo: (ms: number, options?: { play?: boolean }) => void
  seekBy: (deltaMs: number) => void
  toggle: () => void
  setRate: (rate: number) => void
}

const AudioPlayerContext = createContext<AudioPlayerContextValue | null>(null)

const RATE_KEY = 'echoic:playback-rate'

function readStoredRate(): number {
  try {
    const raw = Number(localStorage.getItem(RATE_KEY))
    return [0.75, 1, 1.25, 1.5, 2].includes(raw) ? raw : 1
  } catch {
    return 1
  }
}

interface AudioPlayerProviderProps {
  recordingId: number
  durationMs: number
  mediaType?: 'audio' | 'video'
  /** Override the media URL (used by public share pages). */
  src?: string
  children: ReactNode
}

/**
 * Wraps the native <audio>/<video> element and exposes a shared playback cursor
 * so the player card, transcript, viewpoint tree, chapters and AI citations stay
 * in sync. Video recordings render a real <video> element inside the player card;
 * audio recordings keep a hidden <audio> element in the provider.
 */
export function AudioPlayerProvider({
  recordingId,
  durationMs: apiDuration,
  mediaType = 'audio',
  src,
  children,
}: AudioPlayerProviderProps) {
  const resolvedSrc = src ?? mediaUrl(recordingId)
  const isVideo = mediaType === 'video'
  const mediaRef = useRef<HTMLMediaElement | null>(null)
  const pendingSeekRef = useRef<number | null>(null)
  const [mediaVersion, setMediaVersion] = useState(0)
  const [ready, setReady] = useState(false)
  const [hasMedia, setHasMedia] = useState(true)
  const [playing, setPlaying] = useState(false)
  const [currentMs, setCurrentMs] = useState(0)
  const [durationMs, setDurationMs] = useState(apiDuration || 0)
  const [rate, setRateState] = useState(readStoredRate)

  useEffect(() => {
    setDurationMs((prev) => (apiDuration > 0 ? apiDuration : prev))
  }, [apiDuration])

  const attachMediaElement = useCallback((element: HTMLMediaElement | null) => {
    // React calls inline callback refs on every render; ignore no-op attach
    // events to avoid a mediaVersion -> render -> ref -> mediaVersion loop.
    if (mediaRef.current === element) return
    mediaRef.current = element
    setMediaVersion((value) => value + 1)
  }, [])

  // Apply restored playback rate as soon as the element exists / metadata loads.
  useEffect(() => {
    if (mediaRef.current) mediaRef.current.playbackRate = rate
  }, [rate, ready, mediaVersion])

  // Bind playback events to whichever element is attached (audio or video).
  useEffect(() => {
    const media = mediaRef.current
    if (!media) return
    const onLoadedMetadata = () => {
      setReady(true)
      setHasMedia(true)
      if (Number.isFinite(media.duration) && media.duration > 0) {
        setDurationMs(Math.round(media.duration * 1000))
      }
      if (pendingSeekRef.current !== null) {
        media.currentTime = pendingSeekRef.current / 1000
        pendingSeekRef.current = null
      }
      media.playbackRate = rate
    }
    const onTimeUpdate = () => setCurrentMs(media.currentTime * 1000)
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    const onEnded = () => setPlaying(false)
    const onError = () => {
      setHasMedia(false)
      setReady(false)
    }
    media.addEventListener('loadedmetadata', onLoadedMetadata)
    media.addEventListener('timeupdate', onTimeUpdate)
    media.addEventListener('play', onPlay)
    media.addEventListener('pause', onPause)
    media.addEventListener('ended', onEnded)
    media.addEventListener('error', onError)
    if (media.readyState >= 1) onLoadedMetadata()
    return () => {
      media.removeEventListener('loadedmetadata', onLoadedMetadata)
      media.removeEventListener('timeupdate', onTimeUpdate)
      media.removeEventListener('play', onPlay)
      media.removeEventListener('pause', onPause)
      media.removeEventListener('ended', onEnded)
      media.removeEventListener('error', onError)
    }
  }, [mediaVersion, rate])

  const seekTo = useCallback(
    (ms: number, options?: { play?: boolean }) => {
      const target = Math.max(0, Math.min(ms, Number.isFinite(durationMs) ? durationMs : ms))
      const media = mediaRef.current
      setCurrentMs(target)
      if (!hasMedia || !media) {
        if (!hasMedia) toast.info('该录音暂无媒体文件，无法定位播放')
        return
      }
      if (!ready) {
        pendingSeekRef.current = target
      } else {
        media.currentTime = target / 1000
      }
      if (options?.play) {
        void media.play().catch(() => {
          toast.warning('浏览器阻止了自动播放，请手动点击播放')
        })
      }
    },
    [durationMs, hasMedia, ready],
  )

  const seekBy = useCallback(
    (deltaMs: number) => {
      const media = mediaRef.current
      const from = ready && media ? media.currentTime * 1000 : currentMs
      seekTo(from + deltaMs)
    },
    [currentMs, ready, seekTo],
  )

  const toggle = useCallback(() => {
    const media = mediaRef.current
    if (!media || !hasMedia) {
      toast.info('该录音暂无媒体文件，无法播放')
      return
    }
    if (media.paused) {
      void media.play().catch(() => toast.warning('播放失败，请稍后重试'))
    } else {
      media.pause()
    }
  }, [hasMedia])

  const setRate = useCallback((next: number) => {
    setRateState(next)
    try {
      localStorage.setItem(RATE_KEY, String(next))
    } catch {
      /* ignore */
    }
    if (mediaRef.current) mediaRef.current.playbackRate = next
  }, [])

  useEffect(() => {
    const media = mediaRef.current
    return () => {
      if (media) media.pause()
    }
  }, [])

  const value = useMemo<AudioPlayerContextValue>(
    () => ({
      mediaUrl: resolvedSrc,
      isVideo,
      attachMediaElement,
      ready,
      hasMedia,
      playing,
      currentMs,
      durationMs,
      rate,
      seekTo,
      seekBy,
      toggle,
      setRate,
    }),
    [
      currentMs,
      durationMs,
      hasMedia,
      isVideo,
      playing,
      rate,
      ready,
      resolvedSrc,
      seekBy,
      seekTo,
      setRate,
      toggle,
      attachMediaElement,
    ],
  )

  return (
    <AudioPlayerContext.Provider value={value}>
      {children}
      {!isVideo && (
        <audio
          ref={attachMediaElement}
          src={resolvedSrc}
          preload="metadata"
          className="echoic-audio-element"
        />
      )}
    </AudioPlayerContext.Provider>
  )
}

export function useAudioPlayer(): AudioPlayerContextValue {
  const context = useContext(AudioPlayerContext)
  if (!context) throw new Error('useAudioPlayer must be used inside AudioPlayerProvider')
  return context
}

/** Returns the id of the segment currently being played (or null). */
export function findActiveSegmentId(
  currentMs: number,
  segments: { id: number; start_ms: number; end_ms: number }[],
): number | null {
  if (!segments.length) return null
  for (let i = 0; i < segments.length; i += 1) {
    const segment = segments[i]
    if (currentMs >= segment.start_ms && currentMs < segment.end_ms) return segment.id
  }
  // Between segments: highlight the closest preceding one for a stable indicator.
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    if (currentMs >= segments[i].start_ms) return segments[i].id
  }
  return segments[0]?.id ?? null
}
