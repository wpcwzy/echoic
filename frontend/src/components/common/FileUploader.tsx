import { useCallback, useMemo, useRef, useState } from 'react'
import { Button, Progress, Tooltip } from 'antd'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  CloudUploadOutlined,
  DeleteOutlined,
  AudioOutlined,
  VideoCameraOutlined,
  InboxOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { api } from '../../api/client'
import { useSettings } from '../../api/queries'
import { useIsAdmin } from '../../utils/roles'
import { humanizeError } from '../../utils/format'
import { toast } from '../../utils/toast'
import type { RecordingSummary } from '../../types'

type UploadStatus = 'pending' | 'uploading' | 'done' | 'error'

interface UploadItem {
  uid: string
  file: File
  title: string
  progress: number
  status: UploadStatus
  error: string | null
  recordingId: number | null
}

interface FileUploaderProps {
  language?: string
  source?: 'upload' | 'browser_recording'
  onUploaded?: (recording: RecordingSummary) => void
  compact?: boolean
  accept?: string
  hint?: string
  autoStart?: boolean
}

let uploadSeq = 0

function stripExtension(name: string): string {
  const index = name.lastIndexOf('.')
  return index > 0 ? name.slice(0, index) : name
}

function isVideo(file: File): boolean {
  return file.type.startsWith('video/')
}

/**
 * Custom drag & drop multi-file uploader. Uploads sequentially so progress and
 * failures stay predictable; each success triggers the parent callback.
 */
export function FileUploader({
  language = 'zh',
  source = 'upload',
  onUploaded,
  compact = false,
  accept = 'audio/*,video/*',
  hint,
  autoStart = true,
}: FileUploaderProps) {
  const isAdmin = useIsAdmin()
  const { data: settings } = useSettings(isAdmin)
  const maxMb = settings?.system.max_upload_mb ?? 2048
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [items, setItems] = useState<UploadItem[]>([])
  const [dragging, setDragging] = useState(false)

  const updateItem = useCallback((uid: string, patch: Partial<UploadItem>) => {
    setItems((prev) => prev.map((item) => (item.uid === uid ? { ...item, ...patch } : item)))
  }, [])

  const uploadOne = useCallback(
    async (item: UploadItem) => {
      updateItem(item.uid, { status: 'uploading', progress: 0, error: null })
      const form = new FormData()
      form.append('file', item.file)
      form.append('title', item.title)
      form.append('language', language)
      form.append('source', source)
      try {
        const recording = await api.upload<RecordingSummary>('/api/recordings/upload', form, (percent) => {
          updateItem(item.uid, { progress: percent })
        })
        updateItem(item.uid, { status: 'done', progress: 100, recordingId: recording.id })
        toast.success(`《${recording.title || item.title}》上传成功，已进入处理队列`)
        onUploaded?.(recording)
      } catch (error) {
        const message = humanizeError(error, '上传失败，请重试')
        updateItem(item.uid, { status: 'error', error: message })
        toast.error(message)
      }
    },
    [language, onUploaded, source, updateItem],
  )

  const enqueue = useCallback(
    (files: File[]) => {
      const accepted: File[] = []
      files.forEach((file) => {
        const maxBytes = maxMb * 1024 * 1024
        if (file.size > maxBytes) {
          toast.error(`《${file.name}》超过 ${maxMb} MB 上传限制`)
          return
        }
        if (!file.type.startsWith('audio/') && !file.type.startsWith('video/') && file.type !== '') {
          toast.error(`《${file.name}》不是受支持的音频或视频文件`)
          return
        }
        accepted.push(file)
      })
      if (!accepted.length) return
      const newItems: UploadItem[] = accepted.map((file) => ({
        uid: `upload-${++uploadSeq}`,
        file,
        title: stripExtension(file.name),
        progress: 0,
        status: 'pending',
        error: null,
        recordingId: null,
      }))
      setItems((prev) => [...prev, ...newItems])
      if (autoStart) {
        void (async () => {
          for (const item of newItems) {
            await uploadOne(item)
          }
        })()
      }
    },
    [autoStart, maxMb, uploadOne],
  )

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      setDragging(false)
      if (event.dataTransfer?.files?.length) {
        enqueue(Array.from(event.dataTransfer.files))
      }
    },
    [enqueue],
  )

  const pendingCount = useMemo(() => items.filter((item) => item.status === 'uploading').length, [items])

  return (
    <div className={`file-uploader ${compact ? 'file-uploader--compact' : ''}`}>
      <div
        className={`file-uploader__dropzone ${dragging ? 'is-dragging' : ''}`}
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            inputRef.current?.click()
          }
        }}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <div className="file-uploader__icon">
          {dragging ? <InboxOutlined /> : <CloudUploadOutlined />}
        </div>
        <div className="file-uploader__title">点击或拖拽文件到此区域上传</div>
        <div className="file-uploader__hint">
          {hint ??
            (isAdmin && settings
              ? `支持音频 / 视频文件，单文件不超过 ${maxMb} MB，可批量选择`
              : '支持音频 / 视频文件，可批量选择（大小限制以管理员配置为准）')}
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          multiple
          hidden
          onChange={(event) => {
            if (event.target.files?.length) enqueue(Array.from(event.target.files))
            event.target.value = ''
          }}
        />
      </div>

      {items.length > 0 && (
        <ul className="upload-list">
          {items.map((item) => (
            <li className={`upload-item upload-item--${item.status}`} key={item.uid}>
              <span className="upload-item__icon">
                {item.status === 'done' ? (
                  <CheckCircleFilled style={{ color: '#52c41a' }} />
                ) : item.status === 'error' ? (
                  <CloseCircleFilled style={{ color: '#ff4d4f' }} />
                ) : isVideo(item.file) ? (
                  <VideoCameraOutlined style={{ color: '#722ed1' }} />
                ) : (
                  <AudioOutlined style={{ color: '#1677ff' }} />
                )}
              </span>
              <div className="upload-item__body">
                <div className="upload-item__row">
                  <span className="upload-item__name" title={item.file.name}>
                    {item.file.name}
                  </span>
                  {item.status === 'uploading' && <span className="upload-item__pct">{item.progress}%</span>}
                  {item.status === 'done' && item.recordingId && (
                    <a className="upload-item__link" href={`/r/${item.recordingId}`}>
                      查看详情
                    </a>
                  )}
                  {item.status === 'error' && <span className="upload-item__error">{item.error}</span>}
                </div>
                {(item.status === 'uploading' || item.status === 'pending') && (
                  <Progress
                    percent={item.progress}
                    size="small"
                    showInfo={false}
                    strokeColor={{ from: '#69b1ff', to: '#1677ff' }}
                  />
                )}
              </div>
              <div className="upload-item__actions">
                {item.status === 'error' && (
                  <Tooltip title="重试">
                    <Button
                      type="text"
                      size="small"
                      icon={<ReloadOutlined />}
                      aria-label="重试上传"
                      onClick={() => void uploadOne(item)}
                    />
                  </Tooltip>
                )}
                {item.status !== 'uploading' && (
                  <Tooltip title="移除">
                    <Button
                      type="text"
                      size="small"
                      icon={<DeleteOutlined />}
                      aria-label="移除文件"
                      onClick={() => setItems((prev) => prev.filter((entry) => entry.uid !== item.uid))}
                    />
                  </Tooltip>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      {items.some((item) => item.status === 'done') && pendingCount === 0 && (
        <div className="upload-list__footer">
          <Button type="link" size="small" onClick={() => setItems((prev) => prev.filter((item) => item.status !== 'done'))}>
            清空已完成
          </Button>
        </div>
      )}
    </div>
  )
}
