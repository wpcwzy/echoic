import { useEffect, useMemo, useState } from 'react'
import { Button, DatePicker, Input, Modal, Segmented, Skeleton, Switch, Tag, Tooltip } from 'antd'
import {
  CheckCircleFilled,
  ClockCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EyeOutlined,
  LinkOutlined,
  LockOutlined,
  ReloadOutlined,
  ShareAltOutlined,
  StopOutlined,
} from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import type { RecordingDetail, ShareInfo, ShareListResponse } from '../../types'
import { useCreateShare, useRecordingShares, useRevokeShare, shareAbsoluteUrl } from '../../api/shares'
import { formatDate, formatDateShort, formatRelative } from '../../utils/format'
import { confirmAction, toast, toastError } from '../../utils/toast'
import { EmptyState } from '../common/EmptyState'
import { ErrorState } from '../common/ErrorState'

type ExpiryPreset = '1h' | '24h' | '7d' | '30d' | 'forever' | 'custom'

const EXPIRY_OPTIONS: { label: string; value: ExpiryPreset }[] = [
  { label: '1 小时', value: '1h' },
  { label: '24 小时', value: '24h' },
  { label: '7 天', value: '7d' },
  { label: '30 天', value: '30d' },
  { label: '永久', value: 'forever' },
  { label: '自定义', value: 'custom' },
]

function presetToDate(preset: ExpiryPreset): Dayjs | null {
  const now = dayjs()
  switch (preset) {
    case '1h':
      return now.add(1, 'hour')
    case '24h':
      return now.add(24, 'hour')
    case '7d':
      return now.add(7, 'day')
    case '30d':
      return now.add(30, 'day')
    default:
      return null
  }
}

function randomPassword(): string {
  const alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
  let value = ''
  for (let i = 0; i < 8; i += 1) value += alphabet[Math.floor(Math.random() * alphabet.length)]
  return value
}

function shareStatus(share: ShareInfo): 'active' | 'expired' | 'revoked' {
  if (share.revoked_at) return 'revoked'
  if (share.expired) return 'expired'
  if (share.expires_at && dayjs(share.expires_at).isBefore(dayjs())) return 'expired'
  return 'active'
}

const STATUS_META = {
  active: { label: '有效', color: 'success' as const },
  expired: { label: '已过期', color: 'warning' as const },
  revoked: { label: '已撤销', color: 'default' as const },
}

function copyText(text: string): void {
  if (navigator.clipboard?.writeText) {
    void navigator.clipboard
      .writeText(text)
      .then(() => toast.success('链接已复制到剪贴板'))
      .catch(() => toast.warning('复制失败，请手动复制'))
    return
  }
  toast.warning('当前浏览器不支持自动复制，请手动复制链接')
}

interface ShareRowProps {
  share: ShareInfo
  revoking: boolean
  onRevoke: (share: ShareInfo) => void
}

function ShareRow({ share, revoking, onRevoke }: ShareRowProps) {
  const status = shareStatus(share)
  const meta = STATUS_META[status]
  const url = shareAbsoluteUrl(share.url_path)
  const disabled = status !== 'active'

  return (
    <div className={`share-item share-item--${status}`}>
      <div className="share-item__top">
        <Tag color={meta.color} className="share-item__status">
          {meta.label}
        </Tag>
        <span className="share-item__path" title={url}>
          <LinkOutlined /> {share.url_path}
        </span>
        <span className="share-item__views">
          <EyeOutlined /> {share.view_count} 次访问
        </span>
      </div>

      <div className="share-item__meta">
        <span>
          <ClockCircleOutlined /> {share.expires_at ? `有效期至 ${formatDate(share.expires_at)}` : '永久有效'}
        </span>
        <span>创建于 {share.created_at ? formatDateShort(share.created_at) : '—'}</span>
        {share.last_viewed_at && <span>最近访问 {formatRelative(share.last_viewed_at)}</span>}
        {share.has_password && (
          <span className="share-item__flag share-item__flag--lock">
            <LockOutlined /> 密码保护
          </span>
        )}
        {share.include_transcript && <span className="share-item__flag">含转录</span>}
        {share.include_insights && <span className="share-item__flag">含 AI 纪要</span>}
        {share.allow_download && <span className="share-item__flag">允许下载</span>}
        {!share.include_transcript && !share.include_insights && <span className="share-item__flag">仅播放</span>}
        {share.created_by && <span className="share-item__creator">由 {share.created_by.display_name || share.created_by.username} 创建</span>}
      </div>

      <div className="share-item__actions">
        <Tooltip title={disabled ? '链接已失效，无法访问' : '复制链接'}>
          <Button size="small" icon={<CopyOutlined />} disabled={disabled} onClick={() => copyText(url)}>
            复制
          </Button>
        </Tooltip>
        {status === 'active' && (
          <Button size="small" danger icon={<StopOutlined />} loading={revoking} onClick={() => onRevoke(share)}>
            撤销
          </Button>
        )}
      </div>
    </div>
  )
}

interface ShareDialogProps {
  open: boolean
  onClose: () => void
  recording: RecordingDetail
}

/**
 * Share link management for a recording: create (password / expiry / flags /
 * download), copy the generated URL, and review / revoke existing links.
 */
export function ShareDialog({ open, onClose, recording }: ShareDialogProps) {
  const sharesQuery = useRecordingShares(recording.id, open)
  const createShare = useCreateShare(recording.id)
  const revokeShare = useRevokeShare(recording.id)

  const [includeTranscript, setIncludeTranscript] = useState(true)
  const [includeInsights, setIncludeInsights] = useState(true)
  const [allowDownload, setAllowDownload] = useState(false)
  const [passwordEnabled, setPasswordEnabled] = useState(false)
  const [password, setPassword] = useState('')
  const [expiryPreset, setExpiryPreset] = useState<ExpiryPreset>('forever')
  const [customExpiry, setCustomExpiry] = useState<Dayjs | null>(null)
  const [createdShare, setCreatedShare] = useState<ShareInfo | null>(null)
  const [revokingId, setRevokingId] = useState<number | null>(null)

  useEffect(() => {
    if (!open) return
    setCreatedShare(null)
    setPassword('')
    setPasswordEnabled(false)
    setExpiryPreset('forever')
    setCustomExpiry(null)
  }, [open])

  const shares: ShareListResponse = sharesQuery.data ?? { items: [], total: 0 }
  const activeCount = useMemo(
    () => shares.items.filter((share) => shareStatus(share) === 'active').length,
    [shares.items],
  )

  const expiryLabel = useMemo(() => {
    if (expiryPreset === 'forever') return '链接将永久有效，可随时撤销'
    if (expiryPreset === 'custom') {
      return customExpiry ? `链接将于 ${formatDate(customExpiry.toISOString())} 过期` : '请选择过期时间'
    }
    const date = presetToDate(expiryPreset)
    return date ? `链接将于 ${formatDate(date.toISOString())} 过期` : ''
  }, [customExpiry, expiryPreset])

  const handleCreate = () => {
    if (passwordEnabled) {
      if (password.length < 4) {
        toast.warning('分享密码至少 4 位')
        return
      }
      if (password.length > 64) {
        toast.warning('分享密码最多 64 位')
        return
      }
    }

    let expiresAt: string | null = null
    if (expiryPreset === 'custom') {
      if (!customExpiry) {
        toast.warning('请选择自定义过期时间')
        return
      }
      if (customExpiry.isBefore(dayjs())) {
        toast.warning('过期时间必须晚于当前时间')
        return
      }
      expiresAt = customExpiry.toISOString()
    } else {
      const date = presetToDate(expiryPreset)
      expiresAt = date ? date.toISOString() : null
    }

    createShare.mutate(
      {
        password: passwordEnabled ? password : undefined,
        expires_at: expiresAt,
        include_transcript: includeTranscript,
        include_insights: includeInsights,
        allow_download: allowDownload,
      },
      {
        onSuccess: (share) => {
          setCreatedShare(share)
          setPassword('')
          toast.success('分享链接已创建，可以复制发给对方了')
        },
        onError: (error) => toastError(error, '创建分享链接失败'),
      },
    )
  }

  const handleRevoke = (share: ShareInfo) => {
    confirmAction({
      title: '撤销这个分享链接？',
      content: '撤销后链接会立即失效，已经打开页面的访客也无法继续访问。此操作无法恢复。',
      danger: true,
      okText: '撤销链接',
      onOk: async () => {
        setRevokingId(share.id)
        try {
          await revokeShare.mutateAsync(share.id)
          toast.success('分享链接已撤销')
          if (createdShare?.id === share.id) setCreatedShare(null)
        } catch (error) {
          toastError(error, '撤销失败')
          throw error
        } finally {
          setRevokingId(null)
        }
      },
    })
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={780}
      className="share-dialog"
      destroyOnClose
      title={
        <div className="share-dialog__title">
          <ShareAltOutlined />
          <span>分享录音</span>
          <span className="share-dialog__subtitle" title={recording.title}>
            {recording.title}
          </span>
        </div>
      }
    >
      {createdShare && (
        <div className="share-created">
          <div className="share-created__head">
            <CheckCircleFilled className="share-created__icon" />
            <div>
              <div className="share-created__title">分享链接已创建</div>
              <div className="share-created__desc">
                链接无需登录即可访问
                {createdShare.has_password ? '，对方需要输入你设置的密码' : ''}。
                {createdShare.has_password ? '密码不会再次显示，请妥善保存并告知对方。' : ''}
              </div>
            </div>
          </div>
          <div className="share-created__link">
            <Input readOnly value={shareAbsoluteUrl(createdShare.url_path)} onFocus={(event) => event.currentTarget.select()} />
            <Button type="primary" icon={<CopyOutlined />} onClick={() => copyText(shareAbsoluteUrl(createdShare.url_path))}>
              复制链接
            </Button>
            <Button icon={<LinkOutlined />} href={shareAbsoluteUrl(createdShare.url_path)} target="_blank" rel="noreferrer noopener">
              预览
            </Button>
          </div>
        </div>
      )}

      <div className="share-dialog__body">
        <section className="share-create">
          <div className="share-section-head">
            <h3>创建新的分享链接</h3>
            <span className="share-section-head__hint">链接对未登录访客开放，可随时撤销</span>
          </div>

          <div className="share-permissions">
            <label className="share-permission">
              <span className="share-permission__body">
                <span className="share-permission__title">包含转录稿</span>
                <span className="share-permission__desc">访客可以查看逐字稿、说话人与时间戳</span>
              </span>
              <Switch checked={includeTranscript} onChange={setIncludeTranscript} />
            </label>
            <label className="share-permission">
              <span className="share-permission__body">
                <span className="share-permission__title">包含 AI 纪要</span>
                <span className="share-permission__desc">访客可以查看 AI 纪要、观点树、章节与关键洞察</span>
              </span>
              <Switch checked={includeInsights} onChange={setIncludeInsights} />
            </label>
            <label className="share-permission">
              <span className="share-permission__body">
                <span className="share-permission__title">允许下载原文件</span>
                <span className="share-permission__desc">关闭时访客只能在线播放，无法下载原始音视频</span>
              </span>
              <Switch checked={allowDownload} onChange={setAllowDownload} />
            </label>
          </div>

          <div className="share-options">
            <div className="share-option">
              <div className="share-option__head">
                <span className="share-option__label">密码保护</span>
                <Switch checked={passwordEnabled} onChange={setPasswordEnabled} size="small" />
              </div>
              {passwordEnabled ? (
                <div className="share-option__password">
                  <Input.Password
                    value={password}
                    maxLength={64}
                    placeholder="设置 4–64 位访问密码"
                    onChange={(event) => setPassword(event.target.value)}
                  />
                  <Button size="small" icon={<ReloadOutlined />} onClick={() => setPassword(randomPassword())}>
                    随机生成
                  </Button>
                </div>
              ) : (
                <div className="share-option__desc">无需密码，拿到链接的人都可以访问</div>
              )}
            </div>

            <div className="share-option">
              <div className="share-option__head">
                <span className="share-option__label">有效期</span>
              </div>
              <Segmented
                size="small"
                className="share-expiry"
                value={expiryPreset}
                options={EXPIRY_OPTIONS}
                onChange={(value) => setExpiryPreset(value as ExpiryPreset)}
              />
              {expiryPreset === 'custom' && (
                <DatePicker
                  showTime={{ format: 'HH:mm' }}
                  format="YYYY-MM-DD HH:mm"
                  className="share-expiry__picker"
                  value={customExpiry}
                  placeholder="选择过期时间"
                  disabledDate={(current) => current.isBefore(dayjs().startOf('day'))}
                  onChange={(value) => setCustomExpiry(value)}
                />
              )}
              <div className="share-option__desc">{expiryLabel}</div>
            </div>
          </div>

          <div className="share-create__footer">
            <span className="share-create__note">
              {includeTranscript || includeInsights
                ? `访客可查看${includeTranscript ? '转录' : ''}${includeTranscript && includeInsights ? '与' : ''}${includeInsights ? 'AI 纪要' : ''}`
                : '访客仅能在线播放录音'}
            </span>
            <Button type="primary" icon={<ShareAltOutlined />} loading={createShare.isPending} onClick={handleCreate}>
              创建分享链接
            </Button>
          </div>
        </section>

        <section className="share-list-section">
          <div className="share-section-head">
            <h3>
              已创建的链接
              <span className="share-section-head__count">{shares.total}</span>
            </h3>
            <span className="share-section-head__hint">{activeCount} 个有效</span>
          </div>

          {sharesQuery.isLoading ? (
            <Skeleton active paragraph={{ rows: 3 }} />
          ) : sharesQuery.isError ? (
            <ErrorState
              compact
              title="分享记录加载失败"
              description={sharesQuery.error?.message || '请稍后重试。'}
              onRetry={() => void sharesQuery.refetch()}
            />
          ) : shares.items.length === 0 ? (
            <EmptyState
              variant="plain"
              compact
              icon={<ShareAltOutlined />}
              title="还没有创建分享链接"
              description="创建后可以把会议录音安全地分享给没有账号的同事或外部伙伴。"
            />
          ) : (
            <div className="share-list">
              {shares.items.map((share) => (
                <ShareRow
                  key={share.id}
                  share={share}
                  revoking={revokingId === share.id}
                  onRevoke={handleRevoke}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      <div className="share-dialog__footer">
        <span className="share-dialog__footer-hint">
          <LockOutlined /> 分享页面不包含 AI 问答，也不会暴露其他用户信息
        </span>
        <Button onClick={onClose}>关闭</Button>
      </div>
    </Modal>
  )
}
