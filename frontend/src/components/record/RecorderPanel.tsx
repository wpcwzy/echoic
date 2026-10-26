import { useMemo, useState } from 'react'
import { Alert, Button, Input, Progress, Select, Tag, Tooltip } from 'antd'
import {
  AudioOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SoundOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { api } from '../../api/client'
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

/**
 * Browser microphone recorder: timer, pause/resume, live level meter, device
 * selection and "save & upload" flow.
 */
export function RecorderPanel({ onUploaded }: RecorderPanelProps) {
  const recorder = useRecorder()
  const [title, setTitle] = useState(defaultTitle)
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)

  const recordingActive = recorder.status === 'recording'
  const paused = recorder.status === 'paused'
  const stopped = recorder.status === 'stopped'
  const busy = recorder.status === 'requesting'

  const statusTag = useMemo(() => {
    if (recordingActive) return <Tag color="processing" icon={<span className="record-pulse-dot" />}>录音中</Tag>
    if (paused) return <Tag color="warning">已暂停</Tag>
    if (stopped) return <Tag color="success" icon={<CheckCircleOutlined />}>录音完成</Tag>
    return <Tag>准备就绪</Tag>
  }, [paused, recordingActive, stopped])

  const handleSave = async () => {
    if (!recorder.blob) return
    const extension = extensionForMime(recorder.mimeType)
    const file = new File([recorder.blob], `recording-${dayjs().format('YYYYMMDD-HHmmss')}.${extension}`, {
      type: recorder.mimeType || 'audio/webm',
    })
    const form = new FormData()
    form.append('file', file)
    form.append('title', title.trim() || defaultTitle())
    form.append('language', 'zh')
    form.append('source', 'browser_recording')
    setUploading(true)
    setProgress(0)
    try {
      const recording = await api.upload<RecordingSummary>('/api/recordings/upload', form, (percent) => setProgress(percent))
      toast.success('录音已上传，正在转写与分析')
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
            <Button size="small" onClick={() => void recorder.start()} loading={busy}>
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

      <div className={`recorder-stage ${recordingActive || paused ? 'is-active' : ''}`}>
        <div className="recorder-timer">{formatMs(recorder.elapsedMs)}</div>
        <div className="recorder-timer__hint">
          {recordingActive ? '正在录音，点击暂停可稍后继续' : paused ? '已暂停，点击继续恢复录音' : stopped ? '录音已完成，可保存上传' : '点击下方按钮开始录音'}
        </div>
        <LevelMeter analyserRef={recorder.analyserRef} active={recordingActive} paused={paused} height={76} />
      </div>

      <div className="recorder-controls">
        {recorder.status === 'idle' && (
          <button
            type="button"
            className="recorder-main-btn"
            onClick={() => void recorder.start()}
            disabled={busy}
            aria-label="开始录音"
          >
            {busy ? <span className="recorder-main-btn__spinner" /> : <AudioOutlined />}
            <span>{busy ? '正在启动…' : '开始录音'}</span>
          </button>
        )}

        {(recordingActive || paused) && (
          <>
            <Button
              size="large"
              icon={paused ? <PlayCircleOutlined /> : <PauseOutlined />}
              onClick={() => (paused ? recorder.resume() : recorder.pause())}
            >
              {paused ? '继续' : '暂停'}
            </Button>
            <Button size="large" type="primary" danger icon={<span className="record-stop-square" />} onClick={recorder.stop}>
              结束录音
            </Button>
          </>
        )}

        {stopped && (
          <>
            <Button
              size="large"
              icon={<DeleteOutlined />}
              disabled={uploading}
              onClick={() => {
                recorder.reset()
                setTitle(defaultTitle())
              }}
            >
              重新录制
            </Button>
            <Button
              size="large"
              type="primary"
              icon={<SaveOutlined />}
              loading={uploading}
              onClick={() => void handleSave()}
            >
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
          </div>
          {uploading && (
            <Progress
              percent={progress}
              size="small"
              strokeColor={{ from: '#69b1ff', to: '#1677ff' }}
              status="active"
            />
          )}
        </div>
      )}

      <div className="recorder-hint">
        首次使用需要授权麦克风。若浏览器未弹出授权提示，请点击地址栏的锁形图标开启权限；录音功能需要通过
        <code>https</code> 或 <code>localhost</code> 访问。
      </div>
    </section>
  )
}
