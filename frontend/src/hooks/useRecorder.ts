import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

export type RecorderStatus = 'idle' | 'requesting' | 'recording' | 'paused' | 'stopped'

export interface RecorderDevice {
  deviceId: string
  label: string
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') return ''
  return MIME_CANDIDATES.find((candidate) => MediaRecorder.isTypeSupported(candidate)) ?? ''
}

function describeMediaError(error: unknown): string {
  const name = (error as { name?: string } | null)?.name ?? ''
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return '麦克风权限被拒绝。请点击浏览器地址栏的锁形图标，允许麦克风权限后重试。'
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return '未检测到麦克风设备，请确认设备已连接。'
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return '麦克风被其他应用占用或无法访问，请关闭其他录音应用后重试。'
  }
  if (name === 'OverconstrainedError') {
    return '所选麦克风不可用，请切换到其他设备。'
  }
  if (name === 'SecurityError') {
    return '录音需要安全上下文：请通过 https 或 localhost 访问页面。'
  }
  return '无法启动录音，请检查麦克风权限后重试。'
}

export interface UseRecorderResult {
  status: RecorderStatus
  elapsedMs: number
  error: string | null
  blob: Blob | null
  mimeType: string
  devices: RecorderDevice[]
  deviceId: string | undefined
  setDeviceId: (deviceId: string) => void
  analyserRef: RefObject<AnalyserNode | null>
  /** Live MediaStream while recording (used by the live-caption PCM tap). */
  getStream: () => MediaStream | null
  refreshDevices: () => Promise<void>
  start: () => Promise<void>
  pause: () => void
  resume: () => void
  stop: () => void
  reset: () => void
}

/**
 * MediaRecorder based voice recorder with pause/resume, a live WebAudio
 * analyser (for the level meter) and device enumeration.
 */
export function useRecorder(): UseRecorderResult {
  const [status, setStatus] = useState<RecorderStatus>('idle')
  const [elapsedMs, setElapsedMs] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [blob, setBlob] = useState<Blob | null>(null)
  const [mimeType, setMimeType] = useState('')
  const [devices, setDevices] = useState<RecorderDevice[]>([])
  const [deviceId, setDeviceIdState] = useState<string | undefined>(undefined)

  const streamRef = useRef<MediaStream | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const timerRef = useRef<number | null>(null)
  const elapsedRef = useRef(0)
  const startedAtRef = useRef(0)
  const mimeRef = useRef('')
  const statusRef = useRef<RecorderStatus>('idle')

  useEffect(() => {
    statusRef.current = status
  }, [status])

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const startTimer = useCallback(() => {
    clearTimer()
    startedAtRef.current = Date.now() - elapsedRef.current
    timerRef.current = window.setInterval(() => {
      const next = Date.now() - startedAtRef.current
      elapsedRef.current = next
      setElapsedMs(next)
    }, 200)
  }, [clearTimer])

  const releaseMedia = useCallback(() => {
    clearTimer()
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      try {
        recorderRef.current.stop()
      } catch {
        /* already stopped */
      }
    }
    recorderRef.current = null
    chunksRef.current = []
    if (analyserRef.current) {
      try {
        analyserRef.current.disconnect()
      } catch {
        /* ignore */
      }
      analyserRef.current = null
    }
    if (audioContextRef.current) {
      const ctx = audioContextRef.current
      audioContextRef.current = null
      void ctx.close().catch(() => undefined)
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    }
  }, [clearTimer])

  const refreshDevices = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      const all = await navigator.mediaDevices.enumerateDevices()
      const inputs = all
        .filter((device) => device.kind === 'audioinput')
        .map((device, index) => ({
          deviceId: device.deviceId,
          label: device.label || `麦克风 ${index + 1}`,
        }))
      setDevices(inputs)
      setDeviceIdState((prev) => {
        if (prev && inputs.some((device) => device.deviceId === prev)) return prev
        return inputs[0]?.deviceId
      })
    } catch {
      /* enumeration can be blocked before permission — ignore */
    }
  }, [])

  useEffect(() => {
    void refreshDevices()
    const mediaDevices = navigator.mediaDevices
    if (!mediaDevices) return
    const handler = () => void refreshDevices()
    mediaDevices.addEventListener('devicechange', handler)
    return () => mediaDevices.removeEventListener('devicechange', handler)
  }, [refreshDevices])

  // Cleanup on unmount without touching React state.
  useEffect(() => {
    return () => {
      clearTimer()
      if (recorderRef.current && recorderRef.current.state !== 'inactive') {
        try {
          recorderRef.current.stop()
        } catch {
          /* ignore */
        }
      }
      if (analyserRef.current) {
        try {
          analyserRef.current.disconnect()
        } catch {
          /* ignore */
        }
      }
      if (audioContextRef.current) void audioContextRef.current.close().catch(() => undefined)
      streamRef.current?.getTracks().forEach((track) => track.stop())
    }
  }, [clearTimer])

  const start = useCallback(async () => {
    setError(null)
    setBlob(null)
    setMimeType('')
    elapsedRef.current = 0
    setElapsedMs(0)

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('当前浏览器不支持录音。请使用最新版 Chrome / Edge / Safari，并通过 https 或 localhost 访问。')
      return
    }
    if (typeof MediaRecorder === 'undefined') {
      setError('当前浏览器不支持 MediaRecorder 录音，请更换浏览器后重试。')
      return
    }

    setStatus('requesting')
    let stream: MediaStream | null = null
    try {
      const constraints: MediaStreamConstraints = {
        audio: deviceId
          ? { deviceId: { exact: deviceId }, echoCancellation: true, noiseSuppression: true }
          : { echoCancellation: true, noiseSuppression: true },
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia(constraints)
      } catch (mediaError) {
        const name = (mediaError as { name?: string } | null)?.name
        if (name === 'OverconstrainedError') {
          stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        } else {
          throw mediaError
        }
      }
      streamRef.current = stream
      await refreshDevices()

      const requestedMime = pickMimeType()
      const recorder = requestedMime ? new MediaRecorder(stream, { mimeType: requestedMime }) : new MediaRecorder(stream)
      recorderRef.current = recorder
      mimeRef.current = recorder.mimeType || requestedMime || 'audio/webm'
      chunksRef.current = []

      const audioContext = new AudioContext()
      audioContextRef.current = audioContext
      if (audioContext.state === 'suspended') void audioContext.resume().catch(() => undefined)
      const source = audioContext.createMediaStreamSource(stream)
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.75
      source.connect(analyser)
      analyserRef.current = analyser

      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data && event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.onerror = () => {
        setError('录音过程中发生错误，请重试。')
        setStatus('idle')
        releaseMedia()
      }
      recorder.onstop = () => {
        const type = mimeRef.current
        const recorded = new Blob(chunksRef.current, { type })
        chunksRef.current = []
        setBlob(recorded)
        setMimeType(type)
        setStatus('stopped')
        releaseMedia()
      }

      recorder.start(1000)
      startTimer()
      setStatus('recording')
    } catch (mediaError) {
      stream?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      releaseMedia()
      setError(describeMediaError(mediaError))
      setStatus('idle')
    }
  }, [deviceId, refreshDevices, releaseMedia, startTimer])

  const pause = useCallback(() => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state !== 'recording') return
    recorder.pause()
    elapsedRef.current = Date.now() - startedAtRef.current
    setElapsedMs(elapsedRef.current)
    clearTimer()
    setStatus('paused')
  }, [clearTimer])

  const resume = useCallback(() => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state !== 'paused') return
    recorder.resume()
    startTimer()
    setStatus('recording')
  }, [startTimer])

  const stop = useCallback(() => {
    const recorder = recorderRef.current
    clearTimer()
    if (recorder && recorder.state !== 'inactive') {
      if (recorder.state === 'recording') {
        elapsedRef.current = Date.now() - startedAtRef.current
        setElapsedMs(elapsedRef.current)
      }
      try {
        recorder.stop()
      } catch {
        setStatus('stopped')
        releaseMedia()
      }
    } else {
      setStatus('stopped')
      releaseMedia()
    }
  }, [clearTimer, releaseMedia])

  const reset = useCallback(() => {
    releaseMedia()
    elapsedRef.current = 0
    setElapsedMs(0)
    setBlob(null)
    setMimeType('')
    setError(null)
    setStatus('idle')
  }, [releaseMedia])

  const setDeviceId = useCallback((next: string) => {
    setDeviceIdState(next)
  }, [])

  const getStream = useCallback(() => streamRef.current, [])

  return {
    status,
    elapsedMs,
    error,
    blob,
    mimeType,
    devices,
    deviceId,
    setDeviceId,
    analyserRef,
    getStream,
    refreshDevices,
    start,
    pause,
    resume,
    stop,
    reset,
  }
}
