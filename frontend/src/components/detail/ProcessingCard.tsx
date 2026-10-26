import { Button, Modal, Progress, Tooltip } from 'antd'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  ClockCircleOutlined,
  LoadingOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { stageHint, stageLabel } from '../../theme'
import type { RecordingDetail } from '../../types'
import { formatBytes, formatDate, formatMs } from '../../utils/format'

const STEPS = ['preparing', 'transcribing', 'analyzing', 'embedding', 'finalizing', 'done'] as const

interface ProcessingCardProps {
  recording: RecordingDetail
  onRetry?: () => void
  onCancel?: () => void
  retryLoading?: boolean
}

/**
 * Shown while a recording is uploading/queued/processing or after a failure:
 * stage timeline, progress bar and retry/cancel affordances.
 */
export function ProcessingCard({ recording, onRetry, onCancel, retryLoading }: ProcessingCardProps) {
  const cancelled = recording.status === 'cancelled'
  const failed = recording.status === 'failed'
  const activeStepIndex = Math.max(0, STEPS.indexOf(recording.stage as (typeof STEPS)[number]))
  const progress = Math.max(0, Math.min(100, Math.round(recording.progress || 0)))
  const stage = stageLabel[recording.stage] ?? '处理中'

  return (
    <section className={`processing-card ${failed ? 'processing-card--failed' : ''}`}>
      <div className="processing-card__glow" aria-hidden="true" />
      <div className="processing-card__body">
        <div className={`processing-card__icon ${failed ? 'is-failed' : cancelled ? 'is-cancelled' : ''}`}>
          {failed ? <CloseCircleFilled /> : cancelled ? <StopOutlined /> : <LoadingOutlined spin />}
        </div>
        <h2 className="processing-card__title">
          {cancelled
            ? '处理已取消'
            : failed
              ? '录音处理失败'
              : recording.status === 'queued' || recording.status === 'uploaded'
                ? '已加入处理队列'
                : `正在处理：${stage}`}
        </h2>
        <p className="processing-card__desc">
          {cancelled
            ? '本次处理已被取消，你可以重新发起处理，已上传的媒体文件仍会保留。'
            : failed
              ? recording.error_message || '处理过程中出现异常，你可以重新处理或联系管理员。'
              : stageHint[recording.stage] ?? '正在准备处理，请稍候…'}
        </p>

        {!failed && (
          <>
            <div className="processing-card__progress">
              <Progress
                percent={progress}
                showInfo={false}
                strokeColor={{ from: '#69b1ff', to: '#1677ff' }}
                trailColor="var(--fill-1)"
              />
              <div className="processing-card__progress-meta">
                <span>
                  {recording.stage === 'transcribing'
                    ? '语音转写是最耗时的环节，长录音可能需要数分钟'
                    : '页面会自动刷新，处理完成后可直接查看转写与 AI 纪要'}
                </span>
                <span className="processing-card__percent">{progress}%</span>
              </div>
            </div>

            <ol className="processing-steps">
              {STEPS.slice(0, 5).map((step, index) => {
                const done = index < activeStepIndex
                const active = index === activeStepIndex
                return (
                  <li key={step} className={`processing-step ${done ? 'is-done' : ''} ${active ? 'is-active' : ''}`}>
                    <span className="processing-step__dot">
                      {done ? <CheckCircleFilled /> : active ? <span className="processing-step__pulse" /> : null}
                    </span>
                    <span className="processing-step__label">{stageLabel[step]}</span>
                  </li>
                )
              })}
            </ol>
          </>
        )}

        <dl className="processing-card__meta">
          <div>
            <dt>文件</dt>
            <dd title={recording.original_filename}>{recording.original_filename || '—'}</dd>
          </div>
          <div>
            <dt>大小</dt>
            <dd>{formatBytes(recording.size_bytes)}</dd>
          </div>
          <div>
            <dt>时长</dt>
            <dd>{formatMs(recording.duration_ms)}</dd>
          </div>
          <div>
            <dt>上传时间</dt>
            <dd>{formatDate(recording.created_at)}</dd>
          </div>
        </dl>

        <div className="processing-card__actions">
          {failed || cancelled ? (
            <>
              {onRetry && (
                <Button type="primary" icon={<ReloadOutlined />} loading={retryLoading} onClick={onRetry}>
                  重新处理
                </Button>
              )}
              <Button onClick={() => window.location.reload()}>刷新页面</Button>
            </>
          ) : (
            <>
              <Button onClick={() => window.location.reload()} icon={<ClockCircleOutlined />}>
                刷新状态
              </Button>
              {onCancel && recording.status !== 'cancelled' && (
                <Tooltip title="取消后本次处理将终止，你可以重新发起处理">
                  <Button danger ghost icon={<StopOutlined />} onClick={onCancel}>
                    取消处理
                  </Button>
                </Tooltip>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  )
}

interface CancelProcessingModalProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  loading?: boolean
}

export function CancelProcessingModal({ open, onClose, onConfirm, loading }: CancelProcessingModalProps) {
  return (
    <Modal open={open} title="取消处理？" onCancel={onClose} onOk={onConfirm} confirmLoading={loading} okText="取消处理" cancelText="继续等待">
      <p style={{ color: '#646a73' }}>取消后当前处理任务会终止，已完成的中间结果可能不可用。你可以在资料库中重新发起处理。</p>
    </Modal>
  )
}
