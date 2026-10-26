import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Button, Input, Modal, Pagination, Select, Segmented, Tooltip } from 'antd'
import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  AudioOutlined,
  BarsOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  InboxOutlined,
  RobotOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import {
  useRecordings,
  useSeedDemo,
  useSettings,
  useSystemFeatures,
  useTags,
} from '../api/queries'
import {
  useBatchCancel,
  useBatchRecordings,
  usePurgeRecording,
  useRestoreRecording,
} from '../api/library'
import { useAuthStore } from '../store/auth'
import { isAdminUser } from '../utils/roles'
import { useDebouncedValue } from '../hooks/useDebounce'
import { formatHours } from '../utils/format'
import { confirmAction, toast, toastError } from '../utils/toast'
import type { BatchActionPayload, RecordingListParams, RecordingSummary } from '../types'
import { RecordingCard, RecordingRow } from '../components/library/RecordingCard'
import { BatchActionBar, BatchFailureList, BatchTagDialog, SelectionRow, UndoTrashBar } from '../components/library/BulkActions'
import { DemoModeBanner } from '../components/common/DemoModeBanner'
import { EmptyState } from '../components/common/EmptyState'
import { ErrorState } from '../components/common/ErrorState'
import { FileUploader } from '../components/common/FileUploader'
import { RecordingGridSkeleton, RecordingListSkeleton, StatsCardsSkeleton } from '../components/common/Skeletons'

const PAGE_SIZE = 12
const ACTIVE_STATUS = ['uploaded', 'queued', 'processing']

const STATUS_OPTIONS = [
  { label: '全部', value: '' },
  { label: '已完成', value: 'ready' },
  { label: '处理中', value: 'processing,queued,uploaded' },
  { label: '失败', value: 'failed' },
]

const SORT_OPTIONS = [
  { label: '最近上传', value: 'recent' },
  { label: '最早上传', value: 'oldest' },
  { label: '按标题排序', value: 'title' },
  { label: '按时长排序', value: 'duration' },
]

interface UndoState {
  ids: number[]
  seconds: number
}

export default function LibraryPage() {
  const navigate = useNavigate()
  const user = useAuthStore((state) => state.user)
  const isAdmin = isAdminUser(user)
  const [searchParams, setSearchParams] = useSearchParams()
  const [searchInput, setSearchInput] = useState(searchParams.get('q') ?? '')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set())
  const [batchTagOpen, setBatchTagOpen] = useState(false)
  const [batchBusy, setBatchBusy] = useState(false)
  const [undoState, setUndoState] = useState<UndoState | null>(null)
  const [restoringId, setRestoringId] = useState<number | null>(null)
  const [purgingId, setPurgingId] = useState<number | null>(null)
  const undoTimers = useRef<{ timeout: number | null; interval: number | null }>({ timeout: null, interval: null })
  const debouncedSearch = useDebouncedValue(searchInput, 300)

  const status = searchParams.get('status') ?? ''
  const tagId = searchParams.get('tag_id') ? Number(searchParams.get('tag_id')) : null
  const sort = (searchParams.get('sort') as RecordingListParams['sort']) ?? 'recent'
  const view = searchParams.get('view') === 'list' ? 'list' : 'grid'
  const page = Number(searchParams.get('page') ?? 1)
  const query = searchParams.get('q') ?? ''
  const trashView = searchParams.get('trashed') === '1'

  const updateParams = (patch: Record<string, string | number | null>) => {
    const next = new URLSearchParams(searchParams)
    Object.entries(patch).forEach(([key, value]) => {
      if (value === null || value === '') next.delete(key)
      else next.set(key, String(value))
    })
    next.delete('page')
    if (typeof patch.page === 'number') next.set('page', String(patch.page))
    setSearchParams(next, { replace: true })
  }

  const switchTrashView = (nextTrash: boolean) => {
    const next = new URLSearchParams(searchParams)
    next.delete('page')
    next.delete('status')
    if (nextTrash) next.set('trashed', '1')
    else next.delete('trashed')
    setSearchParams(next, { replace: true })
  }

  useEffect(() => {
    setSearchInput(query)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  useEffect(() => {
    if (debouncedSearch === query) return
    updateParams({ q: debouncedSearch || null })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch])

  const params: RecordingListParams = useMemo(
    () => ({
      query: query || undefined,
      status: trashView ? undefined : status || undefined,
      tag_id: tagId ?? undefined,
      sort,
      page,
      page_size: PAGE_SIZE,
      trashed: trashView || undefined,
    }),
    [page, query, sort, status, tagId, trashView],
  )

  const recordingsQuery = useRecordings(params)
  const tagsQuery = useTags()
  const settingsQuery = useSettings(isAdmin)
  const seedDemo = useSeedDemo()
  const featuresQuery = useSystemFeatures()
  const retentionDays = featuresQuery.data?.trash.retention_days ?? 30
  const trashCountQuery = useRecordings({ trashed: true, page: 1, page_size: 1 }, !trashView)
  const trashCount = trashCountQuery.data?.total ?? 0

  const batchMutation = useBatchRecordings()
  const restoreOne = useRestoreRecording()
  const purgeOne = usePurgeRecording()
  const cancelBatch = useBatchCancel()

  const stats = recordingsQuery.data?.stats
  const items = recordingsQuery.data?.items ?? []
  const total = recordingsQuery.data?.total ?? 0
  const hasFilter = Boolean(query || status || tagId)
  const demoMode = isAdmin && (settingsQuery.data?.system.demo_mode ?? false)

  const visibleIds = useMemo(() => items.map((item) => item.id), [items])
  const selectedOnPage = useMemo(() => visibleIds.filter((id) => selectedIds.has(id)), [selectedIds, visibleIds])
  const allSelected = visibleIds.length > 0 && selectedOnPage.length === visibleIds.length
  const partiallySelected = selectedOnPage.length > 0 && !allSelected
  const processingSelectedCount = useMemo(
    () => items.filter((item) => selectedIds.has(item.id) && ACTIVE_STATUS.includes(item.status)).length,
    [items, selectedIds],
  )

  // Selection only covers the current page — reset whenever the result set changes.
  useEffect(() => {
    setSelectedIds(new Set())
  }, [page, query, sort, status, tagId, trashView, view])

  useEffect(() => {
    document.title = trashView ? '回收站 · Echoic' : '资料库 · Echoic'
  }, [trashView])

  useEffect(() => {
    return () => {
      if (undoTimers.current.timeout !== null) window.clearTimeout(undoTimers.current.timeout)
      if (undoTimers.current.interval !== null) window.clearInterval(undoTimers.current.interval)
    }
  }, [])

  const clearUndoTimers = () => {
    if (undoTimers.current.timeout !== null) window.clearTimeout(undoTimers.current.timeout)
    if (undoTimers.current.interval !== null) window.clearInterval(undoTimers.current.interval)
    undoTimers.current = { timeout: null, interval: null }
  }

  const startUndo = (ids: number[]) => {
    clearUndoTimers()
    setUndoState({ ids, seconds: 5 })
    undoTimers.current.interval = window.setInterval(() => {
      setUndoState((prev) => {
        if (!prev) return null
        const seconds = Math.max(0, prev.seconds - 1)
        return { ...prev, seconds }
      })
    }, 1000)
    undoTimers.current.timeout = window.setTimeout(() => {
      clearUndoTimers()
      setUndoState(null)
    }, 5000)
  }

  const dismissUndo = () => {
    clearUndoTimers()
    setUndoState(null)
  }

  const toggleSelect = (id: number, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const toggleSelectAll = (checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      visibleIds.forEach((id) => {
        if (checked) next.add(id)
        else next.delete(id)
      })
      return next
    })
  }

  const reportBatchResult = (result: { succeeded: number; failed: number; results: { id: number; ok: boolean; error: string | null }[] }, doneLabel: string) => {
    const failures = result.results.filter((item) => !item.ok).map((item) => ({ id: item.id, error: item.error }))
    if (result.failed === 0) {
      toast.success(`${result.succeeded} 条录音${doneLabel}`)
      return
    }
    toast.warning(
      <div className="batch-toast">
        <div className="batch-toast__title">
          {result.succeeded} 条成功，{result.failed} 条失败
        </div>
        <BatchFailureList items={failures} />
      </div>,
    )
  }

  const runBatchAction = (payload: BatchActionPayload, doneLabel: string, undoOnTrash = false) => {
    setBatchBusy(true)
    batchMutation.mutate(payload, {
      onSuccess: (result) => {
        reportBatchResult(result, doneLabel)
        if (undoOnTrash && result.succeeded > 0) {
          const trashedIds = result.results.filter((item) => item.ok).map((item) => item.id)
          startUndo(trashedIds)
        }
        setSelectedIds(new Set())
      },
      onError: (error) => toastError(error, '批量操作失败'),
      onSettled: () => setBatchBusy(false),
    })
  }

  const handleTrashSelected = () => {
    if (!selectedIds.size) return
    runBatchAction({ action: 'trash', recording_ids: [...selectedIds] }, '已移入回收站', true)
  }

  const handleRestoreSelected = () => {
    if (!selectedIds.size) return
    runBatchAction({ action: 'restore', recording_ids: [...selectedIds] }, '已恢复到资料库')
  }

  const handlePurgeSelected = () => {
    if (!selectedIds.size) return
    confirmAction({
      title: `彻底删除所选 ${selectedIds.size} 条录音？`,
      content: '彻底删除后录音文件、转录、AI 分析与分享链接都会被清理，且无法恢复。',
      danger: true,
      okText: '彻底删除',
      onOk: () => runBatchAction({ action: 'purge', recording_ids: [...selectedIds] }, '已彻底删除'),
    })
  }

  const handleReprocess = (from: 'all' | 'asr' | 'insights') => {
    if (!selectedIds.size) return
    if (trashView) {
      toast.warning('回收站中的录音需要先恢复才能重新处理')
      return
    }
    runBatchAction({ action: 'reprocess', recording_ids: [...selectedIds], from }, '已提交重新处理任务')
  }

  const handleCancelProcessing = () => {
    const ids = items.filter((item) => selectedIds.has(item.id) && ACTIVE_STATUS.includes(item.status)).map((item) => item.id)
    if (!ids.length) {
      toast.info('所选录音中没有处理中的任务')
      return
    }
    setBatchBusy(true)
    cancelBatch.mutate(ids, {
      onSuccess: (summary) => {
        if (summary.failed === 0) toast.success(`已取消 ${summary.succeeded} 条录音的处理任务`)
        else toast.warning(`${summary.succeeded} 条已取消，${summary.failed} 条取消失败`)
        setSelectedIds(new Set())
      },
      onError: (error) => toastError(error, '取消处理失败'),
      onSettled: () => setBatchBusy(false),
    })
  }

  const handleBatchTag = (action: 'tag_add' | 'tag_remove', tagIds: number[]) => {
    runBatchAction(
      { action, recording_ids: [...selectedIds], tag_ids: tagIds },
      action === 'tag_add' ? '已添加标签' : '已移除标签',
    )
    setBatchTagOpen(false)
  }

  const handleRestoreOne = (recording: RecordingSummary) => {
    setRestoringId(recording.id)
    restoreOne.mutate(recording.id, {
      onSuccess: () => toast.success(`《${recording.title}》已恢复`),
      onError: (error) => toastError(error, '恢复失败'),
      onSettled: () => setRestoringId(null),
    })
  }

  const handlePurgeOne = (recording: RecordingSummary) => {
    confirmAction({
      title: '彻底删除这条录音？',
      content: `《${recording.title}》的录音文件、转录、AI 分析与分享链接都会被永久删除，无法恢复。`,
      danger: true,
      okText: '彻底删除',
      onOk: async () => {
        setPurgingId(recording.id)
        try {
          await purgeOne.mutateAsync(recording.id)
          toast.success('录音已彻底删除')
        } catch (error) {
          toastError(error, '彻底删除失败')
          throw error
        } finally {
          setPurgingId(null)
        }
      },
    })
  }

  const handleUndoTrash = () => {
    if (!undoState) return
    const ids = undoState.ids
    dismissUndo()
    runBatchAction({ action: 'restore', recording_ids: ids }, '已撤销移入回收站')
  }

  const handleSeedDemo = () => {
    seedDemo.mutate(undefined, {
      onSuccess: (data) => {
        toast.success(data.created > 0 ? '演示数据已就绪' : '演示数据已存在')
      },
      onError: (error) => toastError(error, '载入演示数据失败，请确认管理员权限'),
    })
  }

  return (
    <div className="library-page">
      <DemoModeBanner />

      <section className="library-hero">
        <div className="library-hero__text">
          <div className="library-hero__hello">{user?.display_name || user?.username || '你好'}，欢迎回来</div>
          <h1 className="library-hero__title">{trashView ? '回收站' : '我的录音库'}</h1>
          <p className="library-hero__subtitle">
            {trashView
              ? `已删除的录音会保留 ${retentionDays} 天，可随时恢复或彻底删除`
              : stats
                ? `共 ${stats.total} 条录音 · ${formatHours(stats.total_duration_ms)} 小时内容 · 本周新增 ${stats.this_week} 条`
                : '把每一次会议，变成可检索的知识'}
          </p>
        </div>
        <div className="library-hero__actions">
          {trashView ? (
            <Button size="large" icon={<ArrowLeftOutlined />} onClick={() => switchTrashView(false)}>
              返回资料库
            </Button>
          ) : (
            <>
              <Button size="large" icon={<AudioOutlined />} onClick={() => navigate('/record')}>
                开始录音
              </Button>
              <Button size="large" type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
                上传录音
              </Button>
            </>
          )}
        </div>
      </section>

      {!trashView && (recordingsQuery.isLoading ? (
        <StatsCardsSkeleton />
      ) : (
        <section className="stats-grid">
          <div className="stat-card">
            <div className="stat-card__icon stat-card__icon--blue">
              <DatabaseOutlined />
            </div>
            <div className="stat-card__body">
              <div className="stat-card__label">录音总数</div>
              <div className="stat-card__value">
                {stats?.total ?? 0}
                <span className="stat-card__unit">条</span>
              </div>
              <div className="stat-card__hint">其中 {stats?.ready ?? 0} 条已完成处理</div>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-card__icon stat-card__icon--green">
              <ClockCircleOutlined />
            </div>
            <div className="stat-card__body">
              <div className="stat-card__label">总时长</div>
              <div className="stat-card__value">
                {formatHours(stats?.total_duration_ms ?? 0)}
                <span className="stat-card__unit">小时</span>
              </div>
              <div className="stat-card__hint">累计转写 {stats?.ready ?? 0} 场会议</div>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-card__icon stat-card__icon--purple">
              <ThunderboltOutlined />
            </div>
            <div className="stat-card__body">
              <div className="stat-card__label">本周新增</div>
              <div className="stat-card__value">
                {stats?.this_week ?? 0}
                <span className="stat-card__unit">条</span>
              </div>
              <div className="stat-card__hint">继续记录，让知识持续积累</div>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-card__icon stat-card__icon--orange">
              <RobotOutlined />
            </div>
            <div className="stat-card__body">
              <div className="stat-card__label">AI 处理中</div>
              <div className="stat-card__value">
                {stats?.processing ?? 0}
                <span className="stat-card__unit">条</span>
              </div>
              <div className="stat-card__hint">{(stats?.processing ?? 0) > 0 ? '正在转写与分析，请稍候' : '所有任务已处理完成'}</div>
            </div>
          </div>
        </section>
      ))}

      {trashView && (
        <div className="trash-banner">
          <DeleteOutlined />
          <span>
            回收站中的录音将在删除 {retentionDays} 天后自动彻底删除；恢复后转录、AI 分析与标签关系都会保留。
          </span>
        </div>
      )}

      <section className="library-filters">
        <Input
          className="library-search"
          allowClear
          prefix={<SearchOutlined className="muted-icon" />}
          placeholder={trashView ? '搜索回收站中的录音' : '搜索标题、描述或转录内容'}
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
        />
        {!trashView && (
          <Segmented
            className="library-status-segmented"
            options={STATUS_OPTIONS}
            value={status}
            onChange={(value) => updateParams({ status: String(value) || null })}
          />
        )}
        <Select
          className="library-tag-select"
          allowClear
          placeholder="全部标签"
          value={tagId ?? undefined}
          loading={tagsQuery.isLoading}
          options={(tagsQuery.data?.items ?? []).map((tag) => ({ label: `${tag.name}（${tag.recording_count}）`, value: tag.id }))}
          onChange={(value) => updateParams({ tag_id: value ?? null })}
        />
        <Select
          className="library-sort-select"
          value={sort}
          options={SORT_OPTIONS}
          onChange={(value) => updateParams({ sort: value })}
        />
        <div className="library-filters__spacer" />
        <Button
          className={`library-trash-btn ${trashView ? 'is-active' : ''}`}
          icon={<DeleteOutlined />}
          onClick={() => switchTrashView(!trashView)}
        >
          回收站
          {trashCount > 0 && <span className="library-trash-btn__count">{trashCount}</span>}
        </Button>
        <Tooltip title="切换视图">
          <Segmented
            value={view}
            options={[
              { value: 'grid', icon: <AppstoreOutlined /> },
              { value: 'list', icon: <BarsOutlined /> },
            ]}
            onChange={(value) => updateParams({ view: String(value) })}
          />
        </Tooltip>
      </section>

      <div className="library-result-meta">
        <span>
          {trashView
            ? `回收站 ${total} 条`
            : hasFilter
              ? `筛选结果 ${total} 条`
              : `全部录音 ${total} 条`}
          {query ? ` · 关键词「${query}」` : ''}
        </span>
        {recordingsQuery.isFetching && <span className="library-result-meta__fetching">更新中…</span>}
      </div>

      {items.length > 0 && (
        <SelectionRow
          total={items.length}
          selectedCount={selectedOnPage.length}
          allSelected={allSelected}
          partiallySelected={partiallySelected}
          trashMode={trashView}
          onToggleAll={toggleSelectAll}
          onClear={() => setSelectedIds(new Set())}
        />
      )}

      {recordingsQuery.isError ? (
        <ErrorState
          title="录音列表加载失败"
          description={recordingsQuery.error?.message || '请检查网络连接后重试。'}
          onRetry={() => void recordingsQuery.refetch()}
        />
      ) : recordingsQuery.isLoading ? (
        view === 'grid' ? (
          <RecordingGridSkeleton count={6} />
        ) : (
          <RecordingListSkeleton count={6} />
        )
      ) : items.length === 0 ? (
        <EmptyState
          icon={trashView ? <DeleteOutlined /> : <InboxOutlined />}
          title={
            trashView
              ? hasFilter
                ? '没有匹配的录音'
                : '回收站是空的'
              : hasFilter
                ? '没有匹配的录音'
                : isAdmin
                  ? '还没有任何录音'
                  : '还没有你自己的录音'
          }
          description={
            trashView
              ? hasFilter
                ? '试试调整搜索关键词或筛选条件。'
                : `移入回收站的录音会在这里保留 ${retentionDays} 天，之后自动彻底删除。`
              : hasFilter
                ? '试试调整搜索关键词或筛选条件。'
                : isAdmin
                  ? '上传一段会议录音，或直接使用浏览器开始录音。AI 会自动完成转写、纪要与观点整理。'
                  : '上传或录制第一条录音吧。成员只能看到自己的录音，管理员可以访问全部内容。'
          }
          actions={
            hasFilter ? (
              <Button
                onClick={() => {
                  setSearchInput('')
                  setSearchParams(new URLSearchParams(trashView ? { trashed: '1' } : {}), { replace: true })
                }}
              >
                清除筛选
              </Button>
            ) : trashView ? (
              <Button icon={<ArrowLeftOutlined />} onClick={() => switchTrashView(false)}>
                返回资料库
              </Button>
            ) : (
              <>
                <Button type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
                  上传第一条录音
                </Button>
                <Button icon={<AudioOutlined />} onClick={() => navigate('/record')}>
                  开始录音
                </Button>
                {demoMode && isAdmin && (
                  <Button icon={<ThunderboltOutlined />} loading={seedDemo.isPending} onClick={handleSeedDemo}>
                    载入演示数据
                  </Button>
                )}
              </>
            )
          }
        />
      ) : view === 'grid' ? (
        <div className="recording-grid">
          {items.map((recording) => (
            <RecordingCard
              key={recording.id}
              recording={recording}
              showOwner={isAdmin}
              selectable
              selected={selectedIds.has(recording.id)}
              onSelectToggle={toggleSelect}
              trashMode={trashView}
              retentionDays={retentionDays}
              onRestore={handleRestoreOne}
              onPurge={handlePurgeOne}
              restoring={restoringId === recording.id}
              purging={purgingId === recording.id}
            />
          ))}
        </div>
      ) : (
        <div className="recording-list">
          {items.map((recording) => (
            <RecordingRow
              key={recording.id}
              recording={recording}
              showOwner={isAdmin}
              selectable
              selected={selectedIds.has(recording.id)}
              onSelectToggle={toggleSelect}
              trashMode={trashView}
              retentionDays={retentionDays}
              onRestore={handleRestoreOne}
              onPurge={handlePurgeOne}
              restoring={restoringId === recording.id}
              purging={purgingId === recording.id}
            />
          ))}
        </div>
      )}

      {total > PAGE_SIZE && (
        <div className="library-pagination">
          <Pagination
            current={page}
            pageSize={PAGE_SIZE}
            total={total}
            showSizeChanger={false}
            onChange={(nextPage) => updateParams({ page: nextPage })}
          />
        </div>
      )}

      <Modal
        open={uploadOpen}
        title="上传录音"
        onCancel={() => setUploadOpen(false)}
        footer={[
          <Button key="done" onClick={() => setUploadOpen(false)}>
            完成
          </Button>,
        ]}
        width={640}
      >
        <p className="muted-text">上传后会自动进入转写与 AI 分析队列，可在「录音与上传」页面查看处理进度。</p>
        <FileUploader
          compact
          onUploaded={() => {
            void recordingsQuery.refetch()
          }}
        />
      </Modal>

      {selectedIds.size > 0 && (
        <BatchActionBar
          selectedCount={selectedIds.size}
          trashMode={trashView}
          isAdmin={isAdmin}
          processingCount={processingSelectedCount}
          busy={batchBusy}
          onClear={() => setSelectedIds(new Set())}
          onTrash={handleTrashSelected}
          onRestore={handleRestoreSelected}
          onPurge={handlePurgeSelected}
          onTag={() => setBatchTagOpen(true)}
          onReprocess={handleReprocess}
          onCancelProcessing={handleCancelProcessing}
        />
      )}

      <BatchTagDialog
        open={batchTagOpen}
        tags={tagsQuery.data?.items ?? []}
        loading={batchBusy}
        onCancel={() => setBatchTagOpen(false)}
        onSubmit={handleBatchTag}
      />

      {undoState && (
        <UndoTrashBar
          count={undoState.ids.length}
          secondsLeft={undoState.seconds}
          loading={batchBusy}
          onUndo={handleUndoTrash}
          onDismiss={dismissUndo}
        />
      )}
    </div>
  )
}
