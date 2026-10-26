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
  children: ReactNode
}

/**
 * Wraps the native <audio> element and exposes a shared playback cursor so the
 * player card, transcript, viewpoint tree, chapters and AI citations stay in sync.
 */
export function AudioPlayerProvider({ recordingId, durationMs: apiDuration, children }: AudioPlayerProviderProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const pendingSeekRef = useRef<number | null>(null)
  const [ready, setReady] = useState(false)
  const [hasMedia, setHasMedia] = useState(true)
  const [playing, setPlaying] = useState(false)
  const [currentMs, setCurrentMs] = useState(0)
  const [durationMs, setDurationMs] = useState(apiDuration || 0)
  const [rate, setRateState] = useState(readStoredRate)

  useEffect(() => {
    setDurationMs((prev) => (apiDuration > 0 ? apiDuration : prev))
  }, [apiDuration])

  // Apply restored playback rate as soon as the element exists / metadata loads.
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate
  }, [rate, ready])

  const seekTo = useCallback(
    (ms: number, options?: { play?: boolean }) => {
      const target = Math.max(0, Math.min(ms, Number.isFinite(durationMs) ? durationMs : ms))
      const audio = audioRef.current
      setCurrentMs(target)
      if (!hasMedia || !audio) {
        if (!hasMedia) toast.info('该录音暂无媒体文件，无法定位播放')
        return
      }
      if (!ready) {
        pendingSeekRef.current = target
      } else {
        audio.currentTime = target / 1000
      }
      if (options?.play) {
        void audio.play().catch(() => {
          toast.warning('浏览器阻止了自动播放，请手动点击播放')
        })
      }
    },
    [durationMs, hasMedia, ready],
  )

  const seekBy = useCallback(
    (deltaMs: number) => {
      const audio = audioRef.current
      const from = ready && audio ? audio.currentTime * 1000 : currentMs
      seekTo(from + deltaMs)
    },
    [currentMs, ready, seekTo],
  )

  const toggle = useCallback(() => {
    const audio = audioRef.current
    if (!audio || !hasMedia) {
      toast.info('该录音暂无媒体文件，无法播放')
      return
    }
    if (audio.paused) {
      void audio.play().catch(() => toast.warning('播放失败，请稍后重试'))
    } else {
      audio.pause()
    }
  }, [hasMedia])

  const setRate = useCallback((next: number) => {
    setRateState(next)
    try {
      localStorage.setItem(RATE_KEY, String(next))
    } catch {
      /* ignore */
    }
    if (audioRef.current) audioRef.current.playbackRate = next
  }, [])

  useEffect(() => {
    const audio = audioRef.current
    return () => {
      if (audio) audio.pause()
    }
  }, [])

  const value = useMemo<AudioPlayerContextValue>(
    () => ({
      mediaUrl: mediaUrl(recordingId),
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
    [currentMs, durationMs, hasMedia, playing, rate, ready, recordingId, seekBy, seekTo, setRate, toggle],
  )

  return (
    <AudioPlayerContext.Provider value={value}>
      {children}
      <audio
        ref={audioRef}
        src={mediaUrl(recordingId)}
        preload="metadata"
        className="echoic-audio-element"
        onLoadedMetadata={(event) => {
          const element = event.currentTarget
          setReady(true)
          setHasMedia(true)
          if (Number.isFinite(element.duration) && element.duration > 0) {
            setDurationMs(Math.round(element.duration * 1000))
          }
          if (pendingSeekRef.current !== null) {
            element.currentTime = pendingSeekRef.current / 1000
            pendingSeekRef.current = null
          }
          element.playbackRate = rate
        }}
        onTimeUpdate={(event) => setCurrentMs(event.currentTarget.currentTime * 1000)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={() => {
          setHasMedia(false)
          setReady(false)
        }}
      />
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
