import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { cancelLiveSession, createLiveSession, stopLiveSession } from '../api/live'
import type { LiveSession, LiveWsEvent } from '../types'

export type LiveCaptionStatus =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'stopping'
  | 'ended'
  | 'error'
  | 'unavailable'

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
  /** 1-based counter of the automatic reconnect currently in flight (0 = none). */
  reconnectAttempt: number
  /** Maximum automatic reconnect attempts per outage. */
  maxReconnectAttempts: number
  start: (stream: MediaStream) => Promise<boolean>
  stop: () => Promise<number | null>
  cancel: () => Promise<void>
  setPaused: (paused: boolean) => void
  reset: () => void
}

const SAMPLE_RATE = 16000
const FRAME_SAMPLES = 1600 // 100ms @16kHz
/** Automatic reconnects per outage: 1s / 2s / 4s backoff. */
const MAX_RECONNECT_ATTEMPTS = 3
const RECONNECT_DELAYS_MS = [1000, 2000, 4000] as const
/** A connection that stays healthy this long re-arms the reconnect budget. */
const RETRY_RESET_STABLE_MS = 30_000
const CONNECT_TIMEOUT_MS = 8000
const STOP_TIMEOUT_MS = 8000

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
 * Human-readable reason for a permanent (non-retryable) socket rejection:
 * 4403 = auth/permission (401-equivalent), 4404 = unknown session, 1008 =
 * policy violation. Retrying these would loop against auth failures.
 * 4409 ("not active / second producer") is intentionally retryable: during a
 * half-open network drop the backend may still consider the old socket
 * attached, so a later attempt can resume the same session.
 */
function describeClose(code: number | undefined): string | null {
  switch (code) {
    case 1008:
    case 4403:
      return '实时字幕连接被拒绝（登录已过期或无权访问），请刷新页面重新登录；录音仍会继续'
    case 4404:
      return '实时转写会话不存在或已被清理；录音仍会继续'
    default:
      return null
  }
}

function reconnectDelay(attempt: number): number {
  return RECONNECT_DELAYS_MS[Math.min(Math.max(attempt - 1, 0), RECONNECT_DELAYS_MS.length - 1)]
}

function reconnectExhaustedMessage(lastCloseCode: number | null): string {
  const hint = lastCloseCode === 4409 ? '（会话已结束或存在重复连接）' : ''
  return `实时字幕连接已断开，自动重连 ${MAX_RECONNECT_ATTEMPTS} 次仍未恢复${hint}；录音仍会继续`
}

interface ConnectResult {
  ok: boolean
  /** Present when the socket was rejected before/after opening. */
  closeCode?: number
  timedOut?: boolean
  message?: string
}

/**
 * Live caption client (R-19 GA):
 * - creates a live session, opens the WS from `ws_url` (relative → same host),
 * - taps the recorder MediaStream with WebAudio, resamples to 16k mono PCM16
 *   and streams ~100ms binary frames,
 * - reconnects to the same session/WS URL (1s/2s/4s backoff, max 3 attempts)
 *   when the socket drops unexpectedly while recording; audio captured during
 *   the outage is dropped (never replayed), so frames are not duplicated and
 *   partial captions are cleared once the new socket is up,
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
  const [reconnectAttempt, setReconnectAttempt] = useState(0)

  const statusRef = useRef<LiveCaptionStatus>('idle')
  const wsRef = useRef<WebSocket | null>(null)
  const wsUrlRef = useRef<string | null>(null)
  const sessionRef = useRef<LiveSession | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const sinkRef = useRef<GainNode | null>(null)
  const pausedRef = useRef(false)
  const pendingSamplesRef = useRef<number[]>([])
  const stopResolveRef = useRef<(() => void) | null>(null)
  const stopPromiseRef = useRef<Promise<number | null> | null>(null)
  /** Deliberate close (stop/cancel/unmount): never reconnect. */
  const intentionalCloseRef = useRef(false)
  /** Stop requested by the user: abort reconnects even if a socket is opening. */
  const stopRequestedRef = useRef(false)
  /** Server said `done`: session finalized, never reconnect. */
  const sessionDoneRef = useRef(false)
  const retryCountRef = useRef(0)
  /** Last WS close code seen while live/reconnecting (for the final hint). */
  const lastCloseCodeRef = useRef<number | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const stabilityTimerRef = useRef<number | null>(null)
  const maxTimerRef = useRef<number | null>(null)
  /** Bumped on start/cancel/reset/unmount so stale async work can bail out. */
  const epochRef = useRef(0)
  const lineKeyRef = useRef(0)
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  // Set below so async handlers / timers always call the latest implementation.
  const stopRef = useRef<() => Promise<number | null>>(async () => null)
  const scheduleReconnectRef = useRef<() => void>(() => undefined)
  const attemptReconnectRef = useRef<(attempt: number) => Promise<void>>(async () => undefined)
  const handleUnexpectedCloseRef = useRef<(code: number) => void>(() => undefined)

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

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
  }, [])

  const clearStabilityTimer = useCallback(() => {
    if (stabilityTimerRef.current !== null) {
      window.clearTimeout(stabilityTimerRef.current)
      stabilityTimerRef.current = null
    }
  }, [])

  /** After a connection has been healthy for a while, re-arm reconnect budget. */
  const startStabilityTimer = useCallback(() => {
    clearStabilityTimer()
    stabilityTimerRef.current = window.setTimeout(() => {
      stabilityTimerRef.current = null
      retryCountRef.current = 0
      setReconnectAttempt(0)
    }, RETRY_RESET_STABLE_MS)
  }, [clearStabilityTimer])

  const closeWs = useCallback(() => {
    const ws = wsRef.current
    wsRef.current = null
    if (!ws) return
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
    // While disconnected (reconnecting) audio is dropped on purpose: buffered
    // frames are never replayed, so reconnects cannot duplicate audio and the
    // pending buffer cannot grow without bound.
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      pendingSamplesRef.current = []
      return
    }
    const ratio = inputRate / SAMPLE_RATE
    const outputLength = Math.max(1, Math.floor(samples.length / ratio))
    const pending = pendingSamplesRef.current
    for (let i = 0; i < outputLength; i += 1) {
      const sourceIndex = Math.min(samples.length - 1, Math.floor(i * ratio))
      const value = Math.max(-1, Math.min(1, samples[sourceIndex]))
      pending.push(value < 0 ? value * 0x8000 : value * 0x7fff)
    }
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

  /** Surface a terminal live-caption failure; recording keeps going. */
  const failLive = useCallback(
    (message: string) => {
      clearReconnectTimer()
      clearStabilityTimer()
      setReconnectAttempt(0)
      setError(message)
      setPartialText('')
      pendingSamplesRef.current = []
      setStatusSafe('error')
      cleanupAudio()
      onErrorRef.current?.(message)
    },
    [cleanupAudio, clearReconnectTimer, clearStabilityTimer, setStatusSafe],
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
          // A late `ready` must not resurrect status after stop/cancel.
          if (!stopRequestedRef.current) setStatusSafe('live')
          break
        case 'partial':
          setPartialText(payload.text?.trim() ?? '')
          break
        case 'final':
          appendFinal(payload.text ?? '', payload.start_ms ?? 0, payload.end_ms ?? 0)
          break
        case 'error': {
          const message = payload.message || '实时转写失败，录音仍会继续'
          clearReconnectTimer()
          clearStabilityTimer()
          setReconnectAttempt(0)
          setError(message)
          setStatusSafe('error')
          cleanupAudio()
          onErrorRef.current?.(message)
          break
        }
        case 'done':
          sessionDoneRef.current = true
          clearReconnectTimer()
          clearStabilityTimer()
          setReconnectAttempt(0)
          setPartialText('')
          pendingSamplesRef.current = []
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
    [appendFinal, cleanupAudio, clearMaxTimer, clearReconnectTimer, clearStabilityTimer, setStatusSafe],
  )

  /**
   * Open the session WebSocket and resolve once it is usable (or failed).
   * Reconnects reuse this same helper but never re-create the audio tap.
   */
  const connectSocket = useCallback(
    ({ stream, isReconnect }: { stream: MediaStream | null; isReconnect: boolean }): Promise<ConnectResult> => {
      const url = wsUrlRef.current
      if (!url) return Promise.resolve({ ok: false, message: '实时字幕地址缺失' })
      const epoch = epochRef.current
      return new Promise<ConnectResult>((resolve) => {
        let ws: WebSocket
        try {
          ws = new WebSocket(url)
        } catch {
          resolve({ ok: false })
          return
        }
        let settled = false
        let timer: number | null = null
        wsRef.current = ws
        ws.binaryType = 'arraybuffer'

        const finish = (result: ConnectResult) => {
          if (settled) return
          settled = true
          if (timer !== null) {
            window.clearTimeout(timer)
            timer = null
          }
          resolve(result)
        }
        const aborted = () => stopRequestedRef.current || epochRef.current !== epoch
        const detach = () => {
          ws.onopen = null
          ws.onmessage = null
          ws.onerror = null
          ws.onclose = null
          if (wsRef.current === ws) wsRef.current = null
          try {
            ws.close()
          } catch {
            /* ignore */
          }
        }

        ws.onmessage = handleWsMessage
        ws.onopen = () => {
          if (timer !== null) {
            window.clearTimeout(timer)
            timer = null
          }
          if (aborted() || wsRef.current !== ws) {
            detach()
            finish({ ok: false })
            return
          }
          if (isReconnect) {
            // New socket: drop the stale partial line and any queued audio.
            setPartialText('')
            pendingSamplesRef.current = []
          }
          try {
            ws.send(JSON.stringify({ type: 'start', sample_rate: SAMPLE_RATE, channels: 1 }))
          } catch {
            /* ignore */
          }
          if (!isReconnect) {
            if (!stream) {
              detach()
              finish({ ok: false, message: '麦克风音频流不可用' })
              return
            }
            try {
              startAudioTap(stream)
            } catch (audioError) {
              detach()
              finish({ ok: false, message: describeError(audioError) })
              return
            }
          }
          setStatusSafe('live')
          startStabilityTimer()
          finish({ ok: true })
        }
        ws.onerror = () => {
          // The close event that follows carries the actual code; only fail
          // early when no close is expected to arrive.
          if (!settled) {
            detach()
            finish({ ok: false })
          }
        }
        ws.onclose = (event: CloseEvent) => {
          if (!settled) {
            detach()
            finish({ ok: false, closeCode: event.code })
            return
          }
          if (wsRef.current !== ws) return
          wsRef.current = null
          if (intentionalCloseRef.current || stopRequestedRef.current || sessionDoneRef.current) return
          if (statusRef.current !== 'live') return
          handleUnexpectedCloseRef.current(event.code)
        }
        timer = window.setTimeout(() => {
          if (settled) return
          detach()
          finish({ ok: false, timedOut: true })
        }, CONNECT_TIMEOUT_MS)
      })
    },
    [handleWsMessage, setStatusSafe, startAudioTap, startStabilityTimer],
  )

  const scheduleReconnect = useCallback(() => {
    retryCountRef.current += 1
    const attempt = retryCountRef.current
    setReconnectAttempt(attempt)
    setError(null)
    setPartialText('')
    pendingSamplesRef.current = []
    setStatusSafe('reconnecting')
    clearReconnectTimer()
    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null
      void attemptReconnectRef.current(attempt)
    }, reconnectDelay(attempt))
  }, [clearReconnectTimer, setStatusSafe])

  const attemptReconnect = useCallback(
    async (attempt: number) => {
      if (stopRequestedRef.current || intentionalCloseRef.current || sessionDoneRef.current) return
      if (!sessionRef.current || !wsUrlRef.current) return
      setStatusSafe('reconnecting')
      const result = await connectSocket({ stream: null, isReconnect: true })
      if (stopRequestedRef.current || sessionDoneRef.current || intentionalCloseRef.current) return
      if (result.ok) return
      if (typeof result.closeCode === 'number') lastCloseCodeRef.current = result.closeCode
      const fatal = describeClose(result.closeCode)
      if (fatal) {
        failLive(fatal)
        return
      }
      if (attempt >= MAX_RECONNECT_ATTEMPTS) {
        failLive(reconnectExhaustedMessage(lastCloseCodeRef.current))
        return
      }
      scheduleReconnect()
    },
    [connectSocket, failLive, scheduleReconnect, setStatusSafe],
  )

  const handleUnexpectedClose = useCallback(
    (code: number) => {
      if (intentionalCloseRef.current || stopRequestedRef.current || sessionDoneRef.current) return
      if (statusRef.current !== 'live') return
      clearStabilityTimer()
      wsRef.current = null
      lastCloseCodeRef.current = code
      setPartialText('')
      pendingSamplesRef.current = []
      const fatal = describeClose(code)
      if (fatal) {
        failLive(fatal)
        return
      }
      if (retryCountRef.current >= MAX_RECONNECT_ATTEMPTS) {
        failLive(reconnectExhaustedMessage(lastCloseCodeRef.current))
        return
      }
      scheduleReconnect()
    },
    [clearStabilityTimer, failLive, scheduleReconnect],
  )

  scheduleReconnectRef.current = scheduleReconnect
  attemptReconnectRef.current = attemptReconnect
  handleUnexpectedCloseRef.current = handleUnexpectedClose

  const detachSession = useCallback(() => {
    setSessionId(null)
    setSession(null)
    sessionRef.current = null
    wsUrlRef.current = null
  }, [])

  const start = useCallback(
    async (stream: MediaStream): Promise<boolean> => {
      if (!enabled) {
        setStatusSafe('unavailable')
        return false
      }
      const current = statusRef.current
      if (current === 'connecting' || current === 'live' || current === 'reconnecting' || current === 'stopping') {
        return current === 'live' || current === 'reconnecting'
      }
      epochRef.current += 1
      const epoch = epochRef.current
      clearReconnectTimer()
      clearStabilityTimer()
      clearMaxTimer()
      stopPromiseRef.current = null
      stopResolveRef.current = null
      retryCountRef.current = 0
      lastCloseCodeRef.current = null
      setReconnectAttempt(0)
      intentionalCloseRef.current = false
      stopRequestedRef.current = false
      sessionDoneRef.current = false
      pausedRef.current = false
      setError(null)
      setPartialText('')
      setLines([])
      lineKeyRef.current = 0
      setStatusSafe('connecting')

      try {
        const created = await createLiveSession({ language, merge_mode: mergeMode })
        if (epochRef.current !== epoch || stopRequestedRef.current) {
          void cancelLiveSession(created.id).catch(() => undefined)
          return false
        }
        sessionRef.current = created
        setSession(created)
        setSessionId(created.id)
        wsUrlRef.current = resolveWsUrl(created.ws_url || `/api/live/sessions/${created.id}/stream`)

        const result = await connectSocket({ stream, isReconnect: false })
        if (epochRef.current !== epoch || stopRequestedRef.current) {
          closeWs()
          cleanupAudio()
          void stopLiveSession(created.id).catch(() => undefined)
          return false
        }
        if (!result.ok) {
          const closeMessage = describeClose(result.closeCode)
          if (closeMessage) throw new Error(closeMessage)
          if (result.message) throw new Error(result.message)
          if (result.timedOut) throw new Error('连接实时字幕服务超时')
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
        if (epochRef.current === epoch) {
          let message = describeError(error)
          if (error instanceof ApiError && error.status === 401) {
            message = '登录已过期或无权使用实时字幕，请重新登录；录音仍会继续'
          }
          setError(message)
          setStatusSafe('error')
          onErrorRef.current?.(message)
        }
        closeWs()
        cleanupAudio()
        return false
      }
    },
    [
      cleanupAudio,
      clearMaxTimer,
      clearReconnectTimer,
      clearStabilityTimer,
      closeWs,
      connectSocket,
      enabled,
      language,
      maxMinutes,
      mergeMode,
      setStatusSafe,
    ],
  )

  const stopInner = useCallback(async (): Promise<number | null> => {
    stopRequestedRef.current = true
    clearMaxTimer()
    clearReconnectTimer()
    clearStabilityTimer()
    setReconnectAttempt(0)
    pausedRef.current = true
    setPartialText('')
    pendingSamplesRef.current = []

    const currentSession = sessionRef.current
    const currentId = currentSession?.id ?? null
    cleanupAudio()

    if (!currentId) {
      closeWs()
      setStatusSafe('ended')
      return null
    }

    const alreadyDone = statusRef.current === 'ended' || sessionDoneRef.current
    const ws = wsRef.current
    if (!alreadyDone && ws && ws.readyState === WebSocket.OPEN) {
      setStatusSafe('stopping')
      try {
        ws.send(JSON.stringify({ type: 'stop' }))
      } catch {
        /* ignore */
      }
      const finished = new Promise<void>((resolve) => {
        stopResolveRef.current = resolve
        window.setTimeout(() => {
          if (stopResolveRef.current === resolve) {
            stopResolveRef.current = null
            resolve()
          }
        }, STOP_TIMEOUT_MS)
      })
      await finished
    }
    closeWs()
    sessionDoneRef.current = true

    // Ensure the server-side session is finalized even when the socket was
    // already gone (reconnect exhaustion, network drop, provider failure).
    if (!alreadyDone) {
      try {
        await stopLiveSession(currentId)
      } catch {
        /* best effort */
      }
    }
    setStatusSafe('ended')
    return currentId
  }, [cleanupAudio, clearMaxTimer, clearReconnectTimer, clearStabilityTimer, closeWs, setStatusSafe])

  const stop = useCallback((): Promise<number | null> => {
    const pending = stopPromiseRef.current
    if (pending) return pending
    const promise = stopInner()
    stopPromiseRef.current = promise
    void promise.finally(() => {
      if (stopPromiseRef.current === promise) stopPromiseRef.current = null
    })
    return promise
  }, [stopInner])

  // Stable refs so the max-minute timer / socket handlers call the latest ones.
  stopRef.current = stop

  const cancel = useCallback(async () => {
    epochRef.current += 1
    stopRequestedRef.current = true
    intentionalCloseRef.current = true
    clearMaxTimer()
    clearReconnectTimer()
    clearStabilityTimer()
    setReconnectAttempt(0)
    retryCountRef.current = 0
    stopResolveRef.current?.()
    stopResolveRef.current = null
    stopPromiseRef.current = null
    pausedRef.current = true
    const currentId = sessionRef.current?.id ?? null
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
  }, [cleanupAudio, clearMaxTimer, clearReconnectTimer, clearStabilityTimer, closeWs, detachSession, setStatusSafe])

  const reset = useCallback(() => {
    epochRef.current += 1
    stopRequestedRef.current = true
    intentionalCloseRef.current = true
    clearMaxTimer()
    clearReconnectTimer()
    clearStabilityTimer()
    setReconnectAttempt(0)
    retryCountRef.current = 0
    stopResolveRef.current?.()
    stopResolveRef.current = null
    stopPromiseRef.current = null
    pausedRef.current = false
    cleanupAudio()
    closeWs()
    detachSession()
    setLines([])
    setPartialText('')
    setError(null)
    setStatusSafe('idle')
  }, [cleanupAudio, clearMaxTimer, clearReconnectTimer, clearStabilityTimer, closeWs, detachSession, setStatusSafe])

  const setPaused = useCallback((paused: boolean) => {
    pausedRef.current = paused
    if (paused) pendingSamplesRef.current = []
  }, [])

  // Cleanup on unmount / navigation: never reconnect, best-effort stop session.
  useEffect(() => {
    return () => {
      epochRef.current += 1
      stopRequestedRef.current = true
      intentionalCloseRef.current = true
      clearMaxTimer()
      clearReconnectTimer()
      clearStabilityTimer()
      cleanupAudio()
      const currentId = sessionRef.current?.id ?? null
      closeWs()
      if (currentId) {
        void stopLiveSession(currentId).catch(() => undefined)
      }
    }
  }, [cleanupAudio, clearMaxTimer, clearReconnectTimer, clearStabilityTimer, closeWs])

  return {
    status,
    error,
    sessionId,
    session,
    lines,
    partialText,
    reconnectAttempt,
    maxReconnectAttempts: MAX_RECONNECT_ATTEMPTS,
    start,
    stop,
    cancel,
    setPaused,
    reset,
  }
}
