import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Input, Progress, Select, Switch, Tag, Tooltip } from 'antd'
import {
  AudioOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  LoadingOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SoundOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { api } from '../../api/client'
import { useSystemFeatures } from '../../api/queries'
import { useLiveCaptions, type LiveCaptionStatus } from '../../hooks/useLiveCaptions'
import { useRecorder } from '../../hooks/useRecorder'
import type { RecordingSummary } from '../../types'
import { formatMs } from '../../utils/format'
import { toast, toastError } from '../../utils/toast'
import { LevelMeter } from './LevelMeter'

interface RecorderPanelProps {
  onUploaded?: (recording: RecordingSummary) => void
}

function extensionForMime(mime: string): string {
  if (mime.includes('mp4')) return 'm4a'
  if (mime.includes('ogg')) return 'ogg'
  return 'webm'
}

function defaultTitle(): string {
  return `录音 ${dayjs().format('MM-DD HH:mm')}`
}

const LIVE_STATUS_META: Record<LiveCaptionStatus, { label: string; className: string }> = {
  idle: { label: '未开始', className: 'idle' },
  connecting: { label: '连接中…', className: 'connecting' },
  live: { label: '实时转写中', className: 'live' },
  stopping: { label: '收尾中…', className: 'connecting' },
  ended: { label: '实时稿已结束', className: 'ended' },
  error: { label: '实时字幕异常', className: 'error' },
  unavailable: { label: '实时字幕不可用', className: 'error' },
}

/**
 * Browser microphone recorder: timer, pause/resume, live level meter, device
 * selection, optional live captions (R-19 Beta) and "save & upload" flow.
 */
export function RecorderPanel({ onUploaded }: RecorderPanelProps) {
  const recorder = useRecorder()
  const featuresQuery = useSystemFeatures()
  const liveFeature = featuresQuery.data?.live
  const liveCapability = Boolean(liveFeature?.enabled)
  const [liveEnabled, setLiveEnabled] = useState(true)
  const [title, setTitle] = useState(defaultTitle)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const liveScrollRef = useRef<HTMLDivElement | null>(null)
  const liveEndRef = useRef<HTMLDivElement | null>(null)

  const live = useLiveCaptions({
    enabled: liveCapability,
    language: 'zh',
    mergeMode: liveFeature?.merge_mode ?? 'replace',
    maxMinutes: liveFeature?.max_minutes ?? 60,
  })

  const recordingActive = recorder.status === 'recording'
  const paused = recorder.status === 'paused'
  const stopped = recorder.status === 'stopped'
  const busy = recorder.status === 'requesting'
  const liveVisible = liveCapability && liveEnabled

  const statusTag = useMemo(() => {
    if (recordingActive) return <Tag color="processing" icon={<span className="record-pulse-dot" />}>录音中</Tag>
    if (paused) return <Tag color="warning">已暂停</Tag>
    if (stopped) return <Tag color="success" icon={<CheckCircleOutlined />}>录音完成</Tag>
    return <Tag>准备就绪</Tag>
  }, [paused, recordingActive, stopped])

  const liveStatusMeta = LIVE_STATUS_META[live.status]

  // Auto-scroll the live caption list while new text arrives.
  useEffect(() => {
    if (!liveVisible) return
    const container = liveScrollRef.current
    if (container) container.scrollTop = container.scrollHeight
    liveEndRef.current?.scrollIntoView({ block: 'nearest' })
  }, [live.lines, live.partialText, liveVisible])

  const handleStart = async () => {
    await recorder.start()
    if (!liveVisible) return
    const stream = recorder.getStream()
    if (!stream) return
    const started = await live.start(stream)
    if (!started) {
      toast.warning('实时字幕不可用，已自动降级为普通录音（录音不受影响）')
    }
  }

  const handlePause = () => {
    if (paused) {
      recorder.resume()
      live.setPaused(false)
    } else {
      recorder.pause()
      live.setPaused(true)
    }
  }

  const handleStop = () => {
    recorder.stop()
    if (live.sessionId) {
      void live.stop()
    }
  }

  const handleReset = () => {
    void live.cancel()
    recorder.reset()
    setTitle(defaultTitle())
  }

  const handleSave = async () => {
    if (!recorder.blob) return
    // Ensure the live session is finalized (idempotent) before binding it.
    let sessionId = live.sessionId
    if (sessionId && live.status !== 'ended') {
      sessionId = await live.stop()
    }
    const extension = extensionForMime(recorder.mimeType)
    const file = new File([recorder.blob], `recording-${dayjs().format('YYYYMMDD-HHmmss')}.${extension}`, {
      type: recorder.mimeType || 'audio/webm',
    })
    const form = new FormData()
    form.append('file', file)
    form.append('title', title.trim() || defaultTitle())
    form.append('language', 'zh')
    form.append('source', 'browser_recording')
    if (sessionId) form.append('live_session_id', String(sessionId))
    setUploading(true)
    setProgress(0)
    try {
      const recording = await api.upload<RecordingSummary>('/api/recordings/upload', form, (percent) => setProgress(percent))
      toast.success(
        sessionId
          ? '录音已上传，实时稿已绑定；离线转写完成后以最终稿为准'
          : '录音已上传，正在转写与分析',
      )
      live.reset()
      recorder.reset()
      setTitle(defaultTitle())
      setProgress(0)
      onUploaded?.(recording)
    } catch (error) {
      toastError(error, '录音上传失败，请重试')
    } finally {
      setUploading(false)
    }
  }

  return (
    <section className="recorder-panel">
      <header className="recorder-panel__head">
        <div className="recorder-panel__title">
          <span className="recorder-panel__title-icon">
            <SoundOutlined />
          </span>
          <div>
            <h2>麦克风录音</h2>
            <p>直接使用浏览器录音，结束后自动上传并开始转写</p>
          </div>
        </div>
        {statusTag}
      </header>

      {recorder.error && (
        <Alert
          type="error"
          showIcon
          className="recorder-panel__alert"
          message="无法使用麦克风"
          description={recorder.error}
          action={
            <Button size="small" onClick={() => void handleStart()} loading={busy}>
              重试
            </Button>
          }
        />
      )}

      <div className="recorder-device">
        <span className="recorder-device__label">输入设备</span>
        <Select
          className="recorder-device__select"
          value={recorder.deviceId}
          disabled={recordingActive || paused || busy}
          placeholder={recorder.devices.length ? '选择麦克风' : '未检测到麦克风'}
          options={recorder.devices.map((device) => ({ label: device.label, value: device.deviceId }))}
          onChange={(value) => recorder.setDeviceId(value)}
        />
        <Tooltip title="刷新设备列表">
          <Button
            type="text"
            icon={<ReloadOutlined />}
            aria-label="刷新设备列表"
            disabled={recordingActive || paused}
            onClick={() => void recorder.refreshDevices()}
          />
        </Tooltip>
      </div>

      {liveCapability && (
        <div className="recorder-live-toggle">
          <div className="recorder-live-toggle__text">
            <div className="recorder-live-toggle__title">
              <ThunderboltOutlined /> 实时字幕
              <span className="beta-chip">Beta</span>
            </div>
            <div className="recorder-live-toggle__hint">
              实时转写不区分说话人，完成后以离线转写为准 · 单次上限 {liveFeature?.max_minutes ?? 60} 分钟
            </div>
          </div>
          <Switch
            checked={liveEnabled}
            disabled={recordingActive || paused || busy}
            onChange={setLiveEnabled}
            aria-label="开启实时字幕"
          />
        </div>
      )}

      <div className={`recorder-stage ${recordingActive || paused ? 'is-active' : ''}`}>
        <div className="recorder-timer">{formatMs(recorder.elapsedMs)}</div>
        <div className="recorder-timer__hint">
          {recordingActive ? '正在录音，点击暂停可稍后继续' : paused ? '已暂停，点击继续恢复录音' : stopped ? '录音已完成，可保存上传' : '点击下方按钮开始录音'}
        </div>
        <LevelMeter analyserRef={recorder.analyserRef} active={recordingActive} paused={paused} height={76} />
      </div>

      {liveVisible && (recordingActive || paused || stopped) && (
        <section className={`live-captions live-captions--${liveStatusMeta.className}`} aria-label="实时字幕">
          <header className="live-captions__head">
            <span className={`live-status live-status--${liveStatusMeta.className}`}>
              {live.status === 'live' ? <span className="live-status__dot" /> : null}
              {live.status === 'connecting' || live.status === 'stopping' ? <LoadingOutlined /> : null}
              {live.status === 'error' ? <CloseCircleOutlined /> : null}
              {live.status === 'ended' ? <CheckCircleOutlined /> : null}
              {liveStatusMeta.label}
            </span>
            <span className="live-captions__meta">
              {live.session?.provider ? `${live.session.provider} · ` : ''}
              {live.lines.length} 段已确定
            </span>
          </header>
          <div className="live-captions__body" ref={liveScrollRef}>
            {live.lines.length === 0 && !live.partialText && (
              <div className="live-captions__empty">
                {live.status === 'error' || live.status === 'unavailable'
                  ? '实时字幕未启动，录音仍会正常保存并离线转写'
                  : '等待语音输入…'}
              </div>
            )}
            {live.lines.map((line) => (
              <div className="live-line" key={line.key}>
                <span className="live-line__time">{formatMs(line.startMs)}</span>
                <span className="live-line__text">{line.text}</span>
              </div>
            ))}
            {live.partialText && (
              <div className="live-line live-line--partial">
                <span className="live-line__time">…</span>
                <span className="live-line__text">{live.partialText}</span>
              </div>
            )}
            <div ref={liveEndRef} />
          </div>
          <div className={`live-captions__note ${live.error ? 'is-error' : ''}`}>
            {live.error || '实时稿（Beta，未区分说话人）；保存后可在录音详情中查看，最终以离线转写为准。'}
          </div>
        </section>
      )}

      <div className="recorder-controls">
        {recorder.status === 'idle' && (
          <button type="button" className="recorder-main-btn" onClick={() => void handleStart()} disabled={busy} aria-label="开始录音">
            {busy ? <span className="recorder-main-btn__spinner" /> : <AudioOutlined />}
            <span>{busy ? '正在启动…' : '开始录音'}</span>
          </button>
        )}

        {(recordingActive || paused) && (
          <>
            <Button size="large" icon={paused ? <PlayCircleOutlined /> : <PauseOutlined />} onClick={handlePause}>
              {paused ? '继续' : '暂停'}
            </Button>
            <Button size="large" type="primary" danger icon={<span className="record-stop-square" />} onClick={handleStop}>
              结束录音
            </Button>
          </>
        )}

        {stopped && (
          <>
            <Button size="large" icon={<DeleteOutlined />} disabled={uploading} onClick={handleReset}>
              重新录制
            </Button>
            <Button size="large" type="primary" icon={<SaveOutlined />} loading={uploading} onClick={() => void handleSave()}>
              保存并上传
            </Button>
          </>
        )}
      </div>

      {stopped && (
        <div className="recorder-save">
          <div className="recorder-save__row">
            <span className="recorder-save__label">录音标题</span>
            <Input
              value={title}
              maxLength={80}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="给这段录音起个名字"
              disabled={uploading}
            />
          </div>
          <div className="recorder-save__meta">
            时长 {formatMs(recorder.elapsedMs)} · 格式 {recorder.mimeType || 'audio/webm'} · 大小{' '}
            {recorder.blob ? `${(recorder.blob.size / 1024 / 1024).toFixed(2)} MB` : '—'}
            {live.sessionId ? ` · 实时稿 #${live.sessionId} 将随录音入库` : ''}
          </div>
          {uploading && (
            <Progress percent={progress} size="small" strokeColor={{ from: '#69b1ff', to: '#1677ff' }} status="active" />
          )}
        </div>
      )}

      <div className="recorder-hint">
        首次使用需要授权麦克风。若浏览器未弹出授权提示，请点击地址栏的锁形图标开启权限；录音功能需要通过
        <code>https</code> 或 <code>localhost</code> 访问。
        {liveCapability && liveEnabled ? '实时字幕为 Beta 功能，连接失败会自动降级为普通录音。' : ''}
      </div>
    </section>
  )
}
