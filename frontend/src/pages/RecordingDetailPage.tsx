import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Button, Drawer, Dropdown, Modal, Select, Skeleton, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  DislikeOutlined,
  DownloadOutlined,
  ExportOutlined,
  InfoCircleOutlined,
  LikeOutlined,
  MoreOutlined,
  LinkOutlined,
  ReloadOutlined,
  RobotOutlined,
  ShareAltOutlined,
  SwapOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import {
  exportUrl,
  mediaUrl,
  qk,
  useCancelRecording,
  useDeleteRecording,
  useInsights,
  useRecording,
  useRegenerateInsights,
  useReprocessRecording,
  useTranscript,
  useUpdateRecording,
} from '../api/queries'
import { useUsers } from '../api/users'
import { AudioPlayerProvider, findActiveSegmentId, useAudioPlayer } from '../hooks/useAudioPlayer'
import { useBreakpoint } from '../hooks/useBreakpoint'
import { useUiStore } from '../store/ui'
import { useAuthStore } from '../store/auth'
import type { DetailTabKey, RecordingDetail } from '../types'
import { parseMsParam } from '../utils/format'
import { isAdminUser } from '../utils/roles'
import { confirmAction, toast, toastError } from '../utils/toast'
import { AudioPlayerCard } from '../components/player/AudioPlayerCard'
import { StatusBadge } from '../components/common/StatusBadge'
import { DemoModeBanner } from '../components/common/DemoModeBanner'
import { ErrorState } from '../components/common/ErrorState'
import { DetailPlayerSkeleton, TabContentSkeleton } from '../components/common/Skeletons'
import { ProcessingCard } from '../components/detail/ProcessingCard'
import { TranscriptTab } from '../components/detail/TranscriptTab'
import { SummaryTab } from '../components/detail/SummaryTab'
import { ViewpointTreeTab } from '../components/detail/ViewpointTreeTab'
import { ChaptersTab } from '../components/detail/ChaptersTab'
import { AIPanel } from '../components/detail/AIPanel'
import { MindMapModal } from '../components/detail/MindMapModal'
import { InsightsModal } from '../components/detail/InsightsModal'
import { ShareDialog } from '../components/detail/ShareDialog'

const TABS: { key: DetailTabKey; label: string }[] = [
  { key: 'transcript', label: '转写' },
  { key: 'summary', label: 'AI纪要' },
  { key: 'viewpoints', label: '观点树' },
  { key: 'chapters', label: '章节' },
]

function parseTab(value: string | null): DetailTabKey | null {
  if (!value) return null
  return TABS.some((tab) => tab.key === value) ? (value as DetailTabKey) : null
}

function triggerDownload(url: string): void {
  const link = document.createElement('a')
  link.href = url
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

export default function RecordingDetailPage() {
  const { id } = useParams<{ id: string }>()
  const recordingId = Number(id)
  const recordingQuery = useRecording(recordingId)
  const recording = recordingQuery.data

  useEffect(() => {
    document.title = recording ? `${recording.title} · Echoic` : '录音详情 · Echoic'
  }, [recording?.title])

  if (!Number.isFinite(recordingId) || recordingId <= 0) return <Navigate to="/" replace />

  if (recordingQuery.isLoading) {
    return (
      <div className="detail-page">
        <div className="detail-main">
          <div className="detail-topbar detail-topbar--skeleton">
            <Skeleton.Button active style={{ width: 260, height: 22 }} />
            <Skeleton.Button active shape="round" style={{ width: 170, height: 32 }} />
          </div>
          <div className="detail-scroll">
            <div className="detail-scroll__inner">
              <DetailPlayerSkeleton />
              <TabContentSkeleton rows={5} />
            </div>
          </div>
        </div>
      </div>
    )
  }

  if (recordingQuery.isError || !recording) {
    return (
      <div className="detail-page detail-page--error">
        <ErrorState
          title="录音加载失败"
          description={recordingQuery.error?.message || '该录音可能已被删除，或服务暂时不可用。'}
          onRetry={() => void recordingQuery.refetch()}
        />
        <Link to="/" className="detail-error__back">
          返回资料库
        </Link>
      </div>
    )
  }

  return (
    <AudioPlayerProvider key={recordingId} recordingId={recordingId} durationMs={recording.duration_ms || 0}>
      <DetailWorkspace key={recordingId} recording={recording} />
    </AudioPlayerProvider>
  )
}

function DetailWorkspace({ recording }: { recording: RecordingDetail }) {
  const id = recording.id
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const breakpoint = useBreakpoint()
  const { currentMs, playing, ready, seekTo } = useAudioPlayer()
  const [searchParams, setSearchParams] = useSearchParams()

  const aiCollapsed = useUiStore((state) => state.aiPanelCollapsed)
  const setAiCollapsed = useUiStore((state) => state.setAiPanelCollapsed)
  const aiDrawerOpen = useUiStore((state) => state.aiDrawerOpen)
  const setAiDrawerOpen = useUiStore((state) => state.setAiDrawerOpen)

  const [tab, setTab] = useState<DetailTabKey>(() => parseTab(searchParams.get('tab')) ?? 'transcript')
  const userTabRef = useRef(false)
  const [mindMapOpen, setMindMapOpen] = useState(false)
  const [insightsOpen, setInsightsOpen] = useState(false)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [highlightSegmentId, setHighlightSegmentId] = useState<number | null>(null)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const [shareOpen, setShareOpen] = useState(false)
  const [transferOpen, setTransferOpen] = useState(false)
  const [transferUserId, setTransferUserId] = useState<number | null>(null)
  const [pollInsights, setPollInsights] = useState(false)
  const pollTimerRef = useRef<number | null>(null)

  const isReady = recording.status === 'ready'
  const transcriptQuery = useTranscript(id, recording.has_transcript || isReady)
  const insightsQuery = useInsights(id, isReady, pollInsights)
  const reprocess = useReprocessRecording(id)
  const cancelRecording = useCancelRecording(id)
  const deleteRecording = useDeleteRecording()
  const regenerate = useRegenerateInsights(id)
  const updateRecording = useUpdateRecording(id)
  const currentUser = useAuthStore((state) => state.user)
  const isAdmin = isAdminUser(currentUser)
  const transferUsersQuery = useUsers({ page_size: 100, include_disabled: false }, isAdmin && transferOpen)

  const insights = insightsQuery.data
  const transcript = transcriptQuery.data
  const speakers = useMemo(
    () => (transcript?.speakers?.length ? transcript.speakers : recording.speakers),
    [transcript?.speakers, recording.speakers],
  )
  const chapters = useMemo(() => insights?.chapters ?? [], [insights?.chapters])
  const segments = useMemo(() => transcript?.segments ?? [], [transcript?.segments])
  const activeSegmentId = useMemo(() => findActiveSegmentId(currentMs, segments), [currentMs, segments])

  // Default tab: honour ?tab=, otherwise land on 观点树 once AI analysis exists.
  useEffect(() => {
    if (userTabRef.current || !isReady) return
    const explicit = parseTab(searchParams.get('tab'))
    if (explicit) {
      setTab(explicit)
      userTabRef.current = true
      return
    }
    if (insights?.viewpoint_tree?.root) {
      setTab('viewpoints')
      userTabRef.current = true
    }
  }, [insights?.viewpoint_tree, isReady, searchParams])

  const changeTab = (next: DetailTabKey) => {
    userTabRef.current = true
    setTab(next)
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params, { replace: true })
  }

  // Seek target from ?t=ms or ?segment=<id> (used by global search results).
  const seekTarget = useMemo(() => {
    const fromParam = parseMsParam(searchParams.get('t'))
    if (fromParam !== null) return fromParam
    const segmentParam = Number(searchParams.get('segment'))
    if (Number.isFinite(segmentParam) && segmentParam > 0 && transcript) {
      const segment = transcript.segments.find((item) => item.id === segmentParam)
      if (segment) return segment.start_ms
    }
    return null
  }, [searchParams, transcript])

  useEffect(() => {
    if (seekTarget === null) return
    const segmentParam = Number(searchParams.get('segment'))
    if (Number.isFinite(segmentParam) && segmentParam > 0 && transcript?.segments.some((item) => item.id === segmentParam)) {
      setHighlightSegmentId(segmentParam)
      if (tab !== 'transcript') changeTab('transcript')
    }
    if (ready) seekTo(seekTarget, { play: searchParams.get('autoplay') === '1' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seekTarget, ready])

  // Refresh AI data when the processing pipeline finishes.
  const prevStatusRef = useRef(recording.status)
  useEffect(() => {
    const previous = prevStatusRef.current
    if (previous !== recording.status) {
      if (recording.status === 'ready') {
        toast.success('处理完成，转写与 AI 纪要已就绪')
        void queryClient.invalidateQueries({ queryKey: qk.transcript(id) })
        void queryClient.invalidateQueries({ queryKey: qk.insights(id) })
      } else if (recording.status === 'failed') {
        toast.error(recording.error_message || '录音处理失败')
      }
      prevStatusRef.current = recording.status
    }
  }, [id, queryClient, recording.error_message, recording.status])

  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current)
    }
  }, [])

  const handleRegenerate = (kinds?: string[]) => {
    regenerate.mutate(kinds, {
      onSuccess: () => {
        toast.success('已提交 AI 生成任务，完成后会自动刷新')
        setPollInsights(true)
        if (pollTimerRef.current !== null) window.clearTimeout(pollTimerRef.current)
        pollTimerRef.current = window.setTimeout(() => setPollInsights(false), 45_000)
      },
      onError: (error) => toastError(error, '提交生成任务失败'),
    })
  }

  const copyShareLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href)
      toast.success('详情链接已复制到剪贴板')
    } catch {
      toast.warning('复制失败，请手动复制浏览器地址栏链接')
    }
  }

  const handleDelete = () => {
    confirmAction({
      title: '删除这条录音？',
      content: `《${recording.title}》的转写、AI 分析与媒体文件都会被删除，且无法恢复。`,
      danger: true,
      okText: '删除',
      onOk: async () => {
        try {
          await deleteRecording.mutateAsync(id)
          toast.success('录音已删除')
          navigate('/')
        } catch (error) {
          toastError(error, '删除失败')
        }
      },
    })
  }

  const moreItems: MenuProps['items'] = [
    { key: 'copy_link', icon: <LinkOutlined />, label: '复制页面链接' },
    ...(isAdmin ? [{ key: 'transfer', icon: <SwapOutlined />, label: '转移归属' } as const] : []),
    { type: 'divider' },
    { key: 'reprocess_asr', icon: <ReloadOutlined />, label: '重新转录' },
    { key: 'reprocess_insights', icon: <ThunderboltOutlined />, label: '重新生成 AI 分析' },
    {
      key: 'export',
      icon: <ExportOutlined />,
      label: '导出转录稿',
      children: [
        { key: 'txt', label: 'TXT 文本' },
        { key: 'srt', label: 'SRT 字幕' },
        { key: 'md', label: 'Markdown' },
        { key: 'json', label: 'JSON 数据' },
      ],
    },
    { type: 'divider' },
    { key: 'delete', icon: <DeleteOutlined />, danger: true, label: '删除录音' },
  ]

  const onMoreClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'copy_link') {
      void copyShareLink()
      return
    }
    if (key === 'transfer') {
      setTransferUserId(recording.owner_id ?? null)
      setTransferOpen(true)
      return
    }
    if (key === 'reprocess_asr') {
      reprocess.mutate('asr', {
        onSuccess: () => toast.success('已提交重新转录任务'),
        onError: (error) => toastError(error, '提交失败'),
      })
      return
    }
    if (key === 'reprocess_insights') {
      reprocess.mutate('insights', {
        onSuccess: () => toast.success('已提交 AI 重新分析任务'),
        onError: (error) => toastError(error, '提交失败'),
      })
      return
    }
    if (key === 'delete') {
      handleDelete()
      return
    }
    if (['txt', 'srt', 'md', 'json'].includes(key)) {
      triggerDownload(exportUrl(id, key as 'txt' | 'srt' | 'md' | 'json'))
    }
  }

  const handleCancelProcessing = () => {
    cancelRecording.mutate(undefined, {
      onSuccess: () => {
        toast.success('已取消处理任务')
        setCancelOpen(false)
      },
      onError: (error) => toastError(error, '取消失败'),
    })
  }

  const handleTransferOwner = () => {
    if (!transferUserId) {
      toast.warning('请选择新的归属用户')
      return
    }
    updateRecording.mutate(
      { owner_id: transferUserId },
      {
        onSuccess: (data) => {
          toast.success(`录音已转移给 ${data.owner?.display_name || data.owner?.username || '新用户'}`)
          setTransferOpen(false)
          setTransferUserId(null)
          void queryClient.invalidateQueries({ queryKey: ['recordings'] })
        },
        onError: (error) => toastError(error, '转移归属失败'),
      },
    )
  }

  const aiInline = breakpoint.isWide && !aiCollapsed
  const hasViewpoints = Boolean(insights?.viewpoint_tree?.root)

  const aiPanelNode = (
    <AIPanel
      recordingId={id}
      enabled={isReady}
      insights={insights}
      onOpenMindMap={() => setMindMapOpen(true)}
      onOpenInsights={() => setInsightsOpen(true)}
      onSeek={(ms) => seekTo(ms)}
      variant={aiInline ? 'inline' : 'drawer'}
      onCollapse={() => {
        if (aiInline) setAiCollapsed(true)
        else setAiDrawerOpen(false)
      }}
    />
  )

  return (
    <div className="detail-page">
      <div className="detail-main">
        <header className="detail-topbar">
          <div className="detail-topbar__left">
            <button
              type="button"
              className="topbar-icon-btn detail-back"
              aria-label="返回资料库"
              onClick={() => navigate('/')}
            >
              <ArrowLeftOutlined />
            </button>
            <nav className="detail-breadcrumb" aria-label="面包屑">
              <Link to="/" className="detail-breadcrumb__link">
                资料库
              </Link>
              <span className="detail-breadcrumb__sep">/</span>
              <span className="detail-breadcrumb__current" title={recording.title}>
                {recording.title}
              </span>
              <Tooltip
                title={
                  <div className="detail-info-tooltip">
                    <div>{recording.description || '这场录音暂无描述'}</div>
                    <div className="detail-info-tooltip__meta">
                      录音时间：{recording.recorded_at ? recording.recorded_at.slice(0, 16).replace('T', ' ') : '—'}
                    </div>
                    {recording.owner && (
                      <div className="detail-info-tooltip__meta">
                        归属：{recording.owner.display_name || recording.owner.username}
                      </div>
                    )}
                  </div>
                }
              >
                <InfoCircleOutlined className="detail-breadcrumb__info" />
              </Tooltip>
              <StatusBadge
                status={recording.status}
                progress={recording.progress}
                stage={recording.stage}
                size="small"
              />
            </nav>
          </div>
          <div className="detail-topbar__actions">
            <Tooltip title="下载原始媒体文件">
              <Button className="detail-pill-btn" icon={<DownloadOutlined />} onClick={() => triggerDownload(mediaUrl(id, true))}>
                <span className="detail-pill-btn__text">下载</span>
              </Button>
            </Tooltip>
            <Button type="primary" icon={<ShareAltOutlined />} onClick={() => setShareOpen(true)}>
              分享
            </Button>
            <Dropdown menu={{ items: moreItems, onClick: onMoreClick }} trigger={['click']} placement="bottomRight">
              <Button type="text" icon={<MoreOutlined />} aria-label="更多操作" />
            </Dropdown>
          </div>
        </header>

        <div className="detail-scroll">
          <div className="detail-scroll__inner">
            <DemoModeBanner />

            {isReady ? (
              <>
                <AudioPlayerCard recording={recording} speakers={speakers} chapters={chapters} />

                <div className="detail-tabs">
                  <div className="detail-tabs__nav" role="tablist" aria-label="录音内容">
                    {TABS.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        role="tab"
                        aria-selected={tab === item.key}
                        className={`detail-tab ${tab === item.key ? 'is-active' : ''}`}
                        onClick={() => changeTab(item.key)}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                  <div className="detail-tabs__extra">
                    <Tooltip title="内容有帮助">
                      <button
                        type="button"
                        className={`detail-feedback__btn ${feedback === 'up' ? 'is-active' : ''}`}
                        aria-label="内容有帮助"
                        onClick={() => {
                          setFeedback('up')
                          toast.success('感谢你的反馈')
                        }}
                      >
                        <LikeOutlined />
                      </button>
                    </Tooltip>
                    <Tooltip title="内容需要改进">
                      <button
                        type="button"
                        className={`detail-feedback__btn ${feedback === 'down' ? 'is-active' : ''}`}
                        aria-label="内容需要改进"
                        onClick={() => {
                          setFeedback('down')
                          toast.success('感谢你的反馈')
                        }}
                      >
                        <DislikeOutlined />
                      </button>
                    </Tooltip>
                  </div>
                </div>

                <div className="detail-tab-content">
                  {tab === 'transcript' && (
                    <TranscriptTab
                      recordingId={id}
                      recordingTitle={recording.title}
                      transcript={transcript}
                      loading={transcriptQuery.isLoading}
                      error={transcriptQuery.isError ? transcriptQuery.error : null}
                      currentMs={currentMs}
                      activeSegmentId={activeSegmentId}
                      highlightSegmentId={highlightSegmentId}
                      playing={playing}
                      onSeek={(ms) => seekTo(ms)}
                      onRetry={() => void transcriptQuery.refetch()}
                    />
                  )}
                  {tab === 'summary' && (
                    <SummaryTab
                      recording={recording}
                      insights={insights}
                      loading={insightsQuery.isLoading}
                      error={insightsQuery.isError ? insightsQuery.error : null}
                      regenerating={regenerate.isPending}
                      onRegenerate={handleRegenerate}
                      onOpenInsights={() => setInsightsOpen(true)}
                      onSeek={(ms) => seekTo(ms)}
                      onRetry={() => void insightsQuery.refetch()}
                    />
                  )}
                  {tab === 'viewpoints' && (
                    <ViewpointTreeTab
                      insights={insights}
                      loading={insightsQuery.isLoading}
                      error={insightsQuery.isError ? insightsQuery.error : null}
                      regenerating={regenerate.isPending}
                      onSeek={(ms) => seekTo(ms)}
                      onRegenerate={() => handleRegenerate(['viewpoint_tree'])}
                      onRetry={() => void insightsQuery.refetch()}
                    />
                  )}
                  {tab === 'chapters' && (
                    <ChaptersTab
                      insights={insights}
                      loading={insightsQuery.isLoading}
                      error={insightsQuery.isError ? insightsQuery.error : null}
                      currentMs={currentMs}
                      playing={playing}
                      regenerating={regenerate.isPending}
                      onSeek={(ms) => seekTo(ms)}
                      onRegenerate={() => handleRegenerate(['chapters'])}
                      onRetry={() => void insightsQuery.refetch()}
                    />
                  )}
                </div>
              </>
            ) : (
              <ProcessingCard
                recording={recording}
                retryLoading={reprocess.isPending}
                onRetry={() =>
                  reprocess.mutate('all', {
                    onSuccess: () => toast.success('已提交重新处理任务'),
                    onError: (error) => toastError(error, '提交失败'),
                  })
                }
                onCancel={() => setCancelOpen(true)}
              />
            )}

            <div className="detail-footer">
              Echoic · 私人会议录音知识库 · {recording.original_filename || '录音'}
            </div>
          </div>
        </div>
      </div>

      {aiInline ? (
        <aside className="detail-ai" aria-label="AI 问答面板">
          {aiPanelNode}
        </aside>
      ) : (
        <>
          <button
            type="button"
            className="ai-float-btn"
            onClick={() => setAiDrawerOpen(true)}
            aria-label="打开 AI 问答"
          >
            <RobotOutlined />
            <span>AI 问答</span>
          </button>
          <Drawer
            open={aiDrawerOpen}
            onClose={() => setAiDrawerOpen(false)}
            placement="right"
            width={Math.min(440, Math.max(300, breakpoint.width - 24))}
            closable={false}
            rootClassName="ai-drawer"
            styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}
          >
            {aiPanelNode}
          </Drawer>
        </>
      )}

      <MindMapModal
        open={mindMapOpen}
        onClose={() => setMindMapOpen(false)}
        root={insights?.viewpoint_tree?.root ?? null}
        title={recording.title}
        onSeek={(ms) => {
          seekTo(ms)
          toast.info(`已跳转到 ${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}`)
        }}
      />

      <InsightsModal
        open={insightsOpen}
        onClose={() => setInsightsOpen(false)}
        insights={insights}
        recordingId={id}
      />

      <ShareDialog open={shareOpen} onClose={() => setShareOpen(false)} recording={recording} />

      <Modal
        open={transferOpen}
        title="转移录音归属"
        okText="确认转移"
        cancelText="取消"
        confirmLoading={updateRecording.isPending}
        onOk={handleTransferOwner}
        onCancel={() => {
          setTransferOpen(false)
          setTransferUserId(null)
        }}
        destroyOnClose
      >
        <p className="muted-text">
          转移后该录音及其转录、AI 分析结果将归属新用户，并出现在对方的资料库中（需要管理员权限）。
        </p>
        <Select
          className="transfer-owner-select"
          showSearch
          optionFilterProp="label"
          placeholder="选择新的归属用户"
          loading={transferUsersQuery.isLoading}
          value={transferUserId ?? undefined}
          options={(transferUsersQuery.data?.items ?? [])
            .filter((candidate) => candidate.id !== recording.owner_id)
            .map((candidate) => ({
              label: `${candidate.display_name || candidate.username}（@${candidate.username}）`,
              value: candidate.id,
            }))}
          onChange={(value) => setTransferUserId(value)}
        />
      </Modal>

      <Modal
        open={cancelOpen}
        title="取消处理？"
        onCancel={() => setCancelOpen(false)}
        onOk={handleCancelProcessing}
        confirmLoading={cancelRecording.isPending}
        okText="取消处理"
        cancelText="继续等待"
      >
        <p className="muted-text">取消后当前处理任务会终止，已生成的部分结果可能不可用。你可以在资料库中重新发起处理。</p>
      </Modal>
    </div>
  )
}
