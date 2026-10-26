import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelLiveSession, createLiveSession, stopLiveSession } from '../api/live'
import type { LiveSession, LiveWsEvent } from '../types'

export type LiveCaptionStatus = 'idle' | 'connecting' | 'live' | 'stopping' | 'ended' | 'error' | 'unavailable'

export interface LiveCaptionLine {
  key: string
  text: string
  startMs: number
  endMs: number
}

interface UseLiveCaptionsOptions {
  /** Global feature flag (features.live.enabled). */
  enabled: boolean
  language?: string
  mergeMode?: 'replace' | 'promote'
  maxMinutes?: number
  onError?: (message: string) => void
}

export interface UseLiveCaptionsResult {
  status: LiveCaptionStatus
  error: string | null
  sessionId: number | null
  session: LiveSession | null
  lines: LiveCaptionLine[]
  partialText: string
  start: (stream: MediaStream) => Promise<boolean>
  stop: () => Promise<number | null>
  cancel: () => Promise<void>
  setPaused: (paused: boolean) => void
  reset: () => void
}

const SAMPLE_RATE = 16000
const FRAME_SAMPLES = 1600 // 100ms @16kHz

function resolveWsUrl(url: string): string {
  if (/^wss?:\/\//i.test(url)) return url
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}${url.startsWith('/') ? url : `/${url}`}`
}

function describeError(error: unknown, fallback = '实时字幕连接失败'): string {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === 'string' && error) return error
  return fallback
}

/**
 * Live transcription client (R-19 Beta):
 * - creates a live session, opens the WS from `ws_url` (relative → same host),
 * - taps the recorder MediaStream with WebAudio, resamples to 16k mono PCM16
 *   and streams ~100ms binary frames,
 * - exposes partial/final captions + session id for the upload binding.
 * Any failure is non-fatal: the normal MediaRecorder upload still works.
 */
export function useLiveCaptions({
  enabled,
  language = 'zh',
  mergeMode = 'replace',
  maxMinutes = 60,
  onError,
}: UseLiveCaptionsOptions): UseLiveCaptionsResult {
  const [status, setStatus] = useState<LiveCaptionStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<number | null>(null)
  const [session, setSession] = useState<LiveSession | null>(null)
  const [lines, setLines] = useState<LiveCaptionLine[]>([])
  const [partialText, setPartialText] = useState('')

  const statusRef = useRef<LiveCaptionStatus>('idle')
  const wsRef = useRef<WebSocket | null>(null)
  const sessionRef = useRef<LiveSession | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const sinkRef = useRef<GainNode | null>(null)
  const pausedRef = useRef(false)
  const pendingSamplesRef = useRef<number[]>([])
  const stopResolveRef = useRef<(() => void) | null>(null)
  const intentionalCloseRef = useRef(false)
  const maxTimerRef = useRef<number | null>(null)
  const lineKeyRef = useRef(0)
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  // Set below (after stopInner) so async handlers always call the latest stop.
  const stopRef = useRef<() => Promise<number | null>>(async () => null)

  const setStatusSafe = useCallback((next: LiveCaptionStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const cleanupAudio = useCallback(() => {
    if (processorRef.current) {
      processorRef.current.onaudioprocess = null
      try {
        processorRef.current.disconnect()
      } catch {
        /* ignore */
      }
      processorRef.current = null
    }
    if (sourceRef.current) {
      try {
        sourceRef.current.disconnect()
      } catch {
        /* ignore */
      }
      sourceRef.current = null
    }
    if (sinkRef.current) {
      try {
        sinkRef.current.disconnect()
      } catch {
        /* ignore */
      }
      sinkRef.current = null
    }
    if (audioContextRef.current) {
      const ctx = audioContextRef.current
      audioContextRef.current = null
      void ctx.close().catch(() => undefined)
    }
    pendingSamplesRef.current = []
  }, [])

  const clearMaxTimer = useCallback(() => {
    if (maxTimerRef.current !== null) {
      window.clearTimeout(maxTimerRef.current)
      maxTimerRef.current = null
    }
  }, [])

  const closeWs = useCallback(() => {
    const ws = wsRef.current
    wsRef.current = null
    if (ws) {
      intentionalCloseRef.current = true
      try {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        ws.close()
      } catch {
        /* ignore */
      }
    }
  }, [])

  const appendFinal = useCallback((text: string, startMs: number, endMs: number) => {
    const trimmed = text.trim()
    if (!trimmed) return
    lineKeyRef.current += 1
    setLines((prev) => [...prev, { key: `live-${lineKeyRef.current}`, text: trimmed, startMs, endMs }])
    setPartialText('')
  }, [])

  const sendFrames = useCallback((samples: Float32Array, inputRate: number) => {
    if (!samples.length || pausedRef.current) return
    const ratio = inputRate / SAMPLE_RATE
    const outputLength = Math.max(1, Math.floor(samples.length / ratio))
    const pending = pendingSamplesRef.current
    for (let i = 0; i < outputLength; i += 1) {
      const sourceIndex = Math.min(samples.length - 1, Math.floor(i * ratio))
      const value = Math.max(-1, Math.min(1, samples[sourceIndex]))
      pending.push(value < 0 ? value * 0x8000 : value * 0x7fff)
    }
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    while (pending.length >= FRAME_SAMPLES) {
      const frame = pending.splice(0, FRAME_SAMPLES)
      ws.send(Int16Array.from(frame).buffer)
    }
  }, [])

  const startAudioTap = useCallback(
    (stream: MediaStream) => {
      const AudioContextCtor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!AudioContextCtor) throw new Error('当前浏览器不支持实时字幕音频采集')
      const context = new AudioContextCtor()
      audioContextRef.current = context
      void context.resume().catch(() => undefined)
      const source = context.createMediaStreamSource(stream)
      sourceRef.current = source
      const processor = context.createScriptProcessor(4096, 1, 1)
      processorRef.current = processor
      const sink = context.createGain()
      sink.gain.value = 0
      sinkRef.current = sink
      processor.onaudioprocess = (event: AudioProcessingEvent) => {
        const input = event.inputBuffer.getChannelData(0)
        sendFrames(input, event.inputBuffer.sampleRate || context.sampleRate)
      }
      source.connect(processor)
      processor.connect(sink)
      sink.connect(context.destination)
    },
    [sendFrames],
  )

  const handleWsMessage = useCallback(
    (event: MessageEvent) => {
      let payload: LiveWsEvent
      try {
        payload = JSON.parse(typeof event.data === 'string' ? event.data : '{}') as LiveWsEvent
      } catch {
        return
      }
      switch (payload.type) {
        case 'ready':
          setStatusSafe('live')
          break
        case 'partial':
          setPartialText(payload.text?.trim() ?? '')
          break
        case 'final':
          appendFinal(payload.text ?? '', payload.start_ms ?? 0, payload.end_ms ?? 0)
          break
        case 'error': {
          const message = payload.message || '实时转写失败'
          setError(message)
          setStatusSafe('error')
          onErrorRef.current?.(message)
          break
        }
        case 'done':
          setPartialText('')
          setStatusSafe('ended')
          clearMaxTimer()
          cleanupAudio()
          stopResolveRef.current?.()
          stopResolveRef.current = null
          break
        default:
          break
      }
    },
    [appendFinal, cleanupAudio, clearMaxTimer, setStatusSafe],
  )

  const detachSession = useCallback(() => {
    setSessionId(null)
    setSession(null)
    sessionRef.current = null
  }, [])

  const start = useCallback(
    async (stream: MediaStream): Promise<boolean> => {
      if (!enabled) {
        setStatusSafe('unavailable')
        return false
      }
      if (statusRef.current === 'connecting' || statusRef.current === 'live' || statusRef.current === 'stopping') {
        return statusRef.current === 'live'
      }
      setError(null)
      setPartialText('')
      setLines([])
      lineKeyRef.current = 0
      pausedRef.current = false
      intentionalCloseRef.current = false
      setStatusSafe('connecting')

      try {
        const created = await createLiveSession({ language, merge_mode: mergeMode })
        sessionRef.current = created
        setSession(created)
        setSessionId(created.id)
        const url = resolveWsUrl(created.ws_url || `/api/live/sessions/${created.id}/stream`)
        const ws = new WebSocket(url)
        wsRef.current = ws
        ws.binaryType = 'arraybuffer'
        ws.onmessage = handleWsMessage

        const connected = await new Promise<boolean>((resolve) => {
          let settled = false
          const timer = window.setTimeout(() => {
            if (settled) return
            settled = true
            resolve(false)
          }, 8000)
          ws.onopen = () => {
            if (settled) return
            settled = true
            window.clearTimeout(timer)
            try {
              ws.send(JSON.stringify({ type: 'start', sample_rate: SAMPLE_RATE, channels: 1 }))
            } catch {
              /* ignore */
            }
            try {
              startAudioTap(stream)
            } catch (audioError) {
              resolve(false)
              ws.close()
              return
            }
            setStatusSafe('live')
            resolve(true)
          }
          ws.onerror = () => {
            if (settled) return
            settled = true
            window.clearTimeout(timer)
            resolve(false)
          }
          ws.onclose = () => {
            if (settled) {
              if (!intentionalCloseRef.current && statusRef.current === 'live') {
                setError('实时字幕连接已断开，录音仍会继续')
                setStatusSafe('error')
                cleanupAudio()
                onErrorRef.current?.('实时字幕连接已断开')
              }
              return
            }
            settled = true
            window.clearTimeout(timer)
            resolve(false)
          }
        })

        if (!connected) {
          throw new Error('实时字幕服务连接失败')
        }

        if (maxMinutes > 0) {
          maxTimerRef.current = window.setTimeout(() => {
            setError(`已达到实时字幕上限（${maxMinutes} 分钟），录音仍会继续`)
            onErrorRef.current?.(`实时字幕已达到 ${maxMinutes} 分钟上限`)
            void stopRef.current()
          }, maxMinutes * 60_000)
        }
        return true
      } catch (error) {
        const message = describeError(error)
        setError(message)
        setStatusSafe('error')
        onErrorRef.current?.(message)
        closeWs()
        cleanupAudio()
        return false
      }
    },
    [cleanupAudio, closeWs, enabled, handleWsMessage, language, maxMinutes, mergeMode, setStatusSafe, startAudioTap],
  )

  const stopInner = useCallback(async (): Promise<number | null> => {
    clearMaxTimer()
    pausedRef.current = true
    const currentSession = sessionRef.current
    const currentId = currentSession?.id ?? null
    if (!currentId) {
      cleanupAudio()
      closeWs()
      setStatusSafe('ended')
      return null
    }
    if (statusRef.current === 'ended' || statusRef.current === 'error' || statusRef.current === 'idle') {
      cleanupAudio()
      closeWs()
      setStatusSafe('ended')
      return currentId
    }

    setStatusSafe('stopping')
    setPartialText('')
    cleanupAudio()

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: 'stop' }))
      } catch {
        /* ignore */
      }
    }

    const finished = new Promise<void>((resolve) => {
      stopResolveRef.current = resolve
      window.setTimeout(() => {
        if (stopResolveRef.current === resolve) {
          stopResolveRef.current = null
          resolve()
        }
      }, 8000)
    })
    await finished
    closeWs()

    // Ensure the server-side session is ended even if the socket died early.
    try {
      await stopLiveSession(currentId)
    } catch {
      /* best effort */
    }
    setStatusSafe('ended')
    return currentId
  }, [cleanupAudio, clearMaxTimer, closeWs, setStatusSafe])

  // Stable ref so the max-minute timer can call stop without ordering issues.
  stopRef.current = stopInner

  const stop = useCallback(() => stopRef.current(), [])

  const cancel = useCallback(async () => {
    clearMaxTimer()
    const currentId = sessionRef.current?.id ?? null
    pausedRef.current = true
    cleanupAudio()
    closeWs()
    detachSession()
    setLines([])
    setPartialText('')
    setError(null)
    setStatusSafe('idle')
    if (currentId) {
      try {
        await cancelLiveSession(currentId)
      } catch {
        /* best effort */
      }
    }
  }, [cleanupAudio, clearMaxTimer, closeWs, detachSession, setStatusSafe])

  const reset = useCallback(() => {
    clearMaxTimer()
    cleanupAudio()
    closeWs()
    detachSession()
    setLines([])
    setPartialText('')
    setError(null)
    setStatusSafe('idle')
  }, [cleanupAudio, clearMaxTimer, closeWs, detachSession, setStatusSafe])

  const setPaused = useCallback((paused: boolean) => {
    pausedRef.current = paused
  }, [])

  // Cleanup on unmount / navigation: best-effort stop of the live session.
  useEffect(() => {
    return () => {
      clearMaxTimer()
      cleanupAudio()
      const currentId = sessionRef.current?.id ?? null
      intentionalCloseRef.current = true
      closeWs()
      if (currentId) {
        void stopLiveSession(currentId).catch(() => undefined)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    status,
    error,
    sessionId,
    session,
    lines,
    partialText,
    start,
    stop,
    cancel,
    setPaused,
    reset,
  }
}
