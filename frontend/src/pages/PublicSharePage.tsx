import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Alert, Button, Input, Skeleton, Tag } from 'antd'
import {
  CalendarOutlined,
  ClockCircleOutlined,
  DownloadOutlined,
  EyeOutlined,
  GlobalOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { ApiError } from '../api/client'
import {
  publicDownloadUrl,
  publicMediaUrl,
  usePublicAccessProbe,
  usePublicInsights,
  usePublicShare,
  usePublicTranscript,
  useUnlockPublicShare,
} from '../api/publicShares'
import { AudioPlayerProvider, findActiveSegmentId, useAudioPlayer } from '../hooks/useAudioPlayer'
import type { Insights, PublicRecording, PublicShareInfo, Transcript } from '../types'
import { formatDate, formatDurationHuman, humanizeError, parseMsParam } from '../utils/format'
import { BrandMark } from '../components/common/BrandMark'
import { AvatarGroup } from '../components/common/AvatarGroup'
import { EmptyState } from '../components/common/EmptyState'
import { PublicAudioPlayer } from '../components/public/PublicAudioPlayer'
import { TranscriptTab } from '../components/detail/TranscriptTab'
import { SummaryTab } from '../components/detail/SummaryTab'
import { ViewpointTreeTab } from '../components/detail/ViewpointTreeTab'
import { ChaptersTab } from '../components/detail/ChaptersTab'
import { InsightsModal } from '../components/detail/InsightsModal'

type PublicTabKey = 'transcript' | 'summary' | 'viewpoints' | 'chapters'
interface PublicTab {
  key: PublicTabKey
  label: string
}

const LANGUAGE_LABEL: Record<string, string> = {
  zh: '中文',
  en: 'English',
  ja: '日本語',
  auto: '自动识别',
}

function availableTabs(share: PublicShareInfo): PublicTab[] {
  const tabs: PublicTab[] = []
  if (share.include_transcript) tabs.push({ key: 'transcript', label: '转写' })
  if (share.include_insights) {
    tabs.push({ key: 'summary', label: 'AI纪要' }, { key: 'viewpoints', label: '观点树' }, { key: 'chapters', label: '章节' })
  }
  return tabs
}

function PublicHeader() {
  return (
    <header className="public-header">
      <div className="public-brand">
        <BrandMark size={30} />
        <span className="public-brand__name">Echoic</span>
        <span className="public-header__divider" />
        <span className="public-header__label">分享的录音</span>
      </div>
      <div className="public-header__right">
        <Tag color="blue" bordered={false}>
          无需登录
        </Tag>
      </div>
    </header>
  )
}

function PublicFooter() {
  return (
    <footer className="public-footer">
      <BrandMark size={20} plain />
      <span>由 Echoic 提供 · 自托管会议录音知识库</span>
    </footer>
  )
}

function PublicFrame({ children }: { children: ReactNode }) {
  return (
    <div className="public-page">
      <PublicHeader />
      <main className="public-main public-main--notice">{children}</main>
      <PublicFooter />
    </div>
  )
}

function PublicInvalidNotice() {
  return (
    <div className="public-notice">
      <div className="public-notice__card">
        <div className="public-notice__icon public-notice__icon--warn">
          <WarningOutlined />
        </div>
        <h1 className="public-notice__title">分享链接不存在或已失效</h1>
        <p className="public-notice__desc">
          链接可能已过期、被分享者撤销，或者地址输入有误。请联系分享者获取新的链接。
        </p>
      </div>
    </div>
  )
}

function PublicErrorNotice({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="public-notice">
      <div className="public-notice__card">
        <div className="public-notice__icon public-notice__icon--warn">
          <WarningOutlined />
        </div>
        <h1 className="public-notice__title">分享页面加载失败</h1>
        <p className="public-notice__desc">网络异常或服务暂时不可用，请稍后重试。</p>
        <Button type="primary" onClick={onRetry}>
          重新加载
        </Button>
      </div>
    </div>
  )
}

function PublicLoading() {
  return (
    <PublicFrame>
      <div className="public-loading">
        <Skeleton active paragraph={{ rows: 2 }} title={{ width: '40%' }} />
        <div className="public-loading__player">
          <Skeleton active paragraph={{ rows: 1 }} />
        </div>
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    </PublicFrame>
  )
}

interface PublicWorkspaceProps {
  token: string
  share: PublicShareInfo
  recording: PublicRecording
  tabs: PublicTab[]
  tab: PublicTabKey
  onTabChange: (tab: PublicTabKey) => void
  transcript: Transcript | undefined
  transcriptLoading: boolean
  transcriptError: unknown
  onRetryTranscript: () => void
  insights: Insights | undefined
  insightsLoading: boolean
  insightsError: unknown
  onRetryInsights: () => void
  onOpenInsights: () => void
}

function PublicWorkspace({
  token,
  share,
  recording,
  tabs,
  tab,
  onTabChange,
  transcript,
  transcriptLoading,
  transcriptError,
  onRetryTranscript,
  insights,
  insightsLoading,
  insightsError,
  onRetryInsights,
  onOpenInsights,
}: PublicWorkspaceProps) {
  const { currentMs, playing, seekTo } = useAudioPlayer()
  const [searchParams] = useSearchParams()
  const autoSeekRef = useRef(false)
  const segments = transcript?.segments ?? []
  const speakers = useMemo(
    () => (transcript?.speakers?.length ? transcript.speakers : recording.speakers),
    [recording.speakers, transcript?.speakers],
  )
  const chapters = useMemo(() => insights?.chapters ?? [], [insights?.chapters])
  const activeSegmentId = useMemo(() => findActiveSegmentId(currentMs, segments), [currentMs, segments])

  // Deep-link support: /s/<token>?t=<ms>
  useEffect(() => {
    if (autoSeekRef.current) return
    const target = parseMsParam(searchParams.get('t'))
    if (target === null) return
    autoSeekRef.current = true
    seekTo(target)
  }, [searchParams, seekTo])

  return (
    <div className="public-workspace">
      <PublicAudioPlayer token={token} speakers={speakers} chapters={chapters} allowDownload={share.allow_download} />

      {tabs.length > 0 && (
        <div className="detail-tabs public-tabs" role="tablist" aria-label="录音内容">
          <div className="detail-tabs__nav">
            {tabs.map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={tab === item.key}
                className={`detail-tab ${tab === item.key ? 'is-active' : ''}`}
                onClick={() => onTabChange(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="public-tabs__badge">
            <EyeOutlined /> 只读分享
          </div>
        </div>
      )}

      <div className="public-tab-content">
        {tab === 'transcript' && share.include_transcript && (
          <TranscriptTab
            recordingId={0}
            recordingTitle={recording.title}
            transcript={transcript}
            loading={transcriptLoading}
            error={transcriptError}
            currentMs={currentMs}
            activeSegmentId={activeSegmentId}
            highlightSegmentId={null}
            playing={playing}
            onSeek={(ms) => seekTo(ms)}
            onRetry={onRetryTranscript}
            readOnly
          />
        )}
        {tab === 'summary' && share.include_insights && (
          <SummaryTab
            recording={{ id: 0, status: 'ready' }}
            insights={insights}
            loading={insightsLoading}
            error={insightsError}
            regenerating={false}
            onRegenerate={() => undefined}
            onOpenInsights={onOpenInsights}
            onSeek={(ms) => seekTo(ms)}
            onRetry={onRetryInsights}
            readOnly
          />
        )}
        {tab === 'viewpoints' && share.include_insights && (
          <ViewpointTreeTab
            insights={insights}
            loading={insightsLoading}
            error={insightsError}
            regenerating={false}
            onSeek={(ms) => seekTo(ms)}
            onRegenerate={() => undefined}
            onRetry={onRetryInsights}
            readOnly
          />
        )}
        {tab === 'chapters' && share.include_insights && (
          <ChaptersTab
            insights={insights}
            loading={insightsLoading}
            error={insightsError}
            currentMs={currentMs}
            playing={playing}
            regenerating={false}
            onSeek={(ms) => seekTo(ms)}
            onRegenerate={() => undefined}
            onRetry={onRetryInsights}
            readOnly
          />
        )}
      </div>
    </div>
  )
}

/**
 * Anonymous public share page (`/s/:token`).
 * Never requires a session and never redirects to /login — password protected
 * shares show an unlock gate instead.
 */
export default function PublicSharePage() {
  const { token = '' } = useParams<{ token: string }>()
  const metaQuery = usePublicShare(token)
  const meta = metaQuery.data
  const share = meta?.share

  const probeQuery = usePublicAccessProbe(token, share)
  const unlockMutation = useUnlockPublicShare(token)
  const [unlocked, setUnlocked] = useState(false)
  const [password, setPassword] = useState('')
  const [unlockError, setUnlockError] = useState<string | null>(null)
  const [tab, setTab] = useState<PublicTabKey>('transcript')
  const [insightsOpen, setInsightsOpen] = useState(false)

  const requiresPassword = Boolean(share?.has_password)
  const contentUnlocked = !requiresPassword || unlocked
  const probing = requiresPassword && !unlocked && probeQuery.isLoading
  const tabs = useMemo(() => (share ? availableTabs(share) : []), [share])

  const transcriptQuery = usePublicTranscript(token, Boolean(share?.include_transcript) && contentUnlocked)
  const insightsQuery = usePublicInsights(token, Boolean(share?.include_insights) && contentUnlocked)

  useEffect(() => {
    if (probeQuery.data) setUnlocked(true)
  }, [probeQuery.data])

  useEffect(() => {
    if (!meta) return
    document.title = `${meta.recording.title} · Echoic 分享`
  }, [meta])

  useEffect(() => {
    if (!tabs.length) return
    if (!tabs.some((item) => item.key === tab)) setTab(tabs[0].key)
  }, [tab, tabs])

  const handleUnlock = () => {
    const value = password.trim()
    if (!value) {
      setUnlockError('请输入访问密码')
      return
    }
    setUnlockError(null)
    unlockMutation.mutate(value, {
      onSuccess: () => {
        setUnlocked(true)
        setPassword('')
      },
      onError: (error) => {
        if (error instanceof ApiError) {
          if (error.status === 401) setUnlockError('密码错误，请重试')
          else if (error.status === 429) setUnlockError('尝试过于频繁，请稍后再试')
          else setUnlockError(error.message || '解锁失败，请稍后重试')
          return
        }
        setUnlockError(humanizeError(error, '解锁失败，请稍后重试'))
      },
    })
  }

  const metaStatus = metaQuery.error instanceof ApiError ? metaQuery.error.status : null
  const isInvalid = metaStatus === 404 || metaStatus === 410 || metaStatus === 403

  if (metaQuery.isLoading) return <PublicLoading />
  if (metaQuery.isError || !meta) {
    return <PublicFrame>{isInvalid ? <PublicInvalidNotice /> : <PublicErrorNotice onRetry={() => void metaQuery.refetch()} />}</PublicFrame>
  }
  if (probing) return <PublicLoading />

  if (requiresPassword && !unlocked) {
    return (
      <PublicFrame>
        <div className="share-gate">
          <div className="share-gate__card">
            <div className="share-gate__icon">
              <LockOutlined />
            </div>
            <h1 className="share-gate__title">该分享链接已加密</h1>
            <p className="share-gate__desc">
              分享者设置了访问密码，输入密码后即可查看
              <strong>《{meta.recording.title}》</strong>。
            </p>
            <Input.Password
              size="large"
              value={password}
              placeholder="请输入访问密码"
              autoFocus
              status={unlockError ? 'error' : undefined}
              onChange={(event) => {
                setPassword(event.target.value)
                if (unlockError) setUnlockError(null)
              }}
              onPressEnter={handleUnlock}
            />
            {unlockError && <Alert type="error" showIcon message={unlockError} className="share-gate__alert" />}
            <Button
              type="primary"
              size="large"
              block
              loading={unlockMutation.isPending}
              onClick={handleUnlock}
              className="share-gate__submit"
            >
              解锁访问
            </Button>
            <div className="share-gate__hint">
              <SafetyCertificateOutlined /> 密码由分享者单独告知，解锁后 2 小时内无需重复输入
            </div>
          </div>
        </div>
      </PublicFrame>
    )
  }

  const recording = meta.recording
  const transcript = transcriptQuery.data
  const insights = insightsQuery.data

  return (
    <div className="public-page">
      <PublicHeader />
      <main className="public-main">
        <section className="public-hero">
          <div className="public-hero__main">
            <h1 className="public-hero__title">{recording.title}</h1>
            {recording.description && <p className="public-hero__desc">{recording.description}</p>}
            <div className="public-meta">
              <span>
                <CalendarOutlined /> {formatDate(recording.recorded_at)}
              </span>
              <span>
                <ClockCircleOutlined /> {formatDurationHuman(recording.duration_ms)}
              </span>
              <span>
                <GlobalOutlined /> {LANGUAGE_LABEL[recording.language] ?? recording.language}
              </span>
              <span>
                <TeamOutlined /> {recording.speakers.length} 位发言人
              </span>
              {recording.tags.map((tag) => (
                <span key={tag.id} className="tag-chip" style={{ color: tag.color, background: `${tag.color}18` }}>
                  {tag.name}
                </span>
              ))}
            </div>
            <div className="public-hero__speakers">
              <AvatarGroup speakers={recording.speakers} max={6} size={26} label="发言人" />
            </div>
          </div>
          <div className="public-hero__actions">
            {share?.allow_download && (
              <Button icon={<DownloadOutlined />} href={publicDownloadUrl(token)}>
                下载原文件
              </Button>
            )}
            <span className="public-hero__views">
              <EyeOutlined /> {share?.view_count ?? 0} 次访问
            </span>
          </div>
        </section>

        <AudioPlayerProvider
          recordingId={0}
          durationMs={recording.duration_ms}
          mediaType={recording.media_type}
          src={publicMediaUrl(token)}
        >
          <PublicWorkspace
            token={token}
            share={share!}
            recording={recording}
            tabs={tabs}
            tab={tab}
            onTabChange={setTab}
            transcript={transcript}
            transcriptLoading={transcriptQuery.isLoading}
            transcriptError={transcriptQuery.isError ? transcriptQuery.error : null}
            onRetryTranscript={() => void transcriptQuery.refetch()}
            insights={insights}
            insightsLoading={insightsQuery.isLoading}
            insightsError={insightsQuery.isError ? insightsQuery.error : null}
            onRetryInsights={() => void insightsQuery.refetch()}
            onOpenInsights={() => setInsightsOpen(true)}
          />
        </AudioPlayerProvider>

        {tabs.length === 0 && (
          <EmptyState
            variant="plain"
            compact
            title="分享者仅开放了在线播放"
            description="该分享未包含转录或 AI 纪要，你仍然可以播放上面的录音。"
          />
        )}

        <div className="public-share-note">
          <LockOutlined /> 本页面为只读分享，不包含 AI 问答，也不会暴露分享者与其他用户的账号信息。
        </div>
      </main>
      <PublicFooter />
      <InsightsModal
        open={insightsOpen}
        onClose={() => setInsightsOpen(false)}
        insights={insights}
        recordingId={0}
      />
    </div>
  )
}
