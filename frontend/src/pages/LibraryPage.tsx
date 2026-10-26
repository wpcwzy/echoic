import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Button, Input, Modal, Pagination, Select, Segmented, Tooltip } from 'antd'
import {
  AppstoreOutlined,
  AudioOutlined,
  BarsOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
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
  useTags,
} from '../api/queries'
import { useAuthStore } from '../store/auth'
import { isAdminUser } from '../utils/roles'
import { useDebouncedValue } from '../hooks/useDebounce'
import { formatHours } from '../utils/format'
import { toast, toastError } from '../utils/toast'
import type { RecordingListParams } from '../types'
import { RecordingCard, RecordingRow } from '../components/library/RecordingCard'
import { DemoModeBanner } from '../components/common/DemoModeBanner'
import { EmptyState } from '../components/common/EmptyState'
import { ErrorState } from '../components/common/ErrorState'
import { FileUploader } from '../components/common/FileUploader'
import { RecordingGridSkeleton, RecordingListSkeleton, StatsCardsSkeleton } from '../components/common/Skeletons'

const PAGE_SIZE = 12

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

export default function LibraryPage() {
  const navigate = useNavigate()
  const user = useAuthStore((state) => state.user)
  const isAdmin = isAdminUser(user)
  const [searchParams, setSearchParams] = useSearchParams()
  const [searchInput, setSearchInput] = useState(searchParams.get('q') ?? '')
  const [uploadOpen, setUploadOpen] = useState(false)
  const debouncedSearch = useDebouncedValue(searchInput, 300)

  const status = searchParams.get('status') ?? ''
  const tagId = searchParams.get('tag_id') ? Number(searchParams.get('tag_id')) : null
  const sort = (searchParams.get('sort') as RecordingListParams['sort']) ?? 'recent'
  const view = searchParams.get('view') === 'list' ? 'list' : 'grid'
  const page = Number(searchParams.get('page') ?? 1)
  const query = searchParams.get('q') ?? ''

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
      status: status || undefined,
      tag_id: tagId ?? undefined,
      sort,
      page,
      page_size: PAGE_SIZE,
    }),
    [page, query, sort, status, tagId],
  )

  const recordingsQuery = useRecordings(params)
  const tagsQuery = useTags()
  const settingsQuery = useSettings(isAdmin)
  const seedDemo = useSeedDemo()
  const stats = recordingsQuery.data?.stats
  const items = recordingsQuery.data?.items ?? []
  const total = recordingsQuery.data?.total ?? 0
  const hasFilter = Boolean(query || status || tagId)
  const demoMode = isAdmin && (settingsQuery.data?.system.demo_mode ?? false)

  useEffect(() => {
    document.title = '资料库 · Echoic'
  }, [])

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
          <h1 className="library-hero__title">我的录音库</h1>
          <p className="library-hero__subtitle">
            {stats
              ? `共 ${stats.total} 条录音 · ${formatHours(stats.total_duration_ms)} 小时内容 · 本周新增 ${stats.this_week} 条`
              : '把每一次会议，变成可检索的知识'}
          </p>
        </div>
        <div className="library-hero__actions">
          <Button size="large" icon={<AudioOutlined />} onClick={() => navigate('/record')}>
            开始录音
          </Button>
          <Button size="large" type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
            上传录音
          </Button>
        </div>
      </section>

      {recordingsQuery.isLoading ? (
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
      )}

      <section className="library-filters">
        <Input
          className="library-search"
          allowClear
          prefix={<SearchOutlined className="muted-icon" />}
          placeholder="搜索标题、描述或转录内容"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
        />
        <Segmented
          className="library-status-segmented"
          options={STATUS_OPTIONS}
          value={status}
          onChange={(value) => updateParams({ status: String(value) || null })}
        />
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
          {hasFilter ? `筛选结果 ${total} 条` : `全部录音 ${total} 条`}
          {query ? ` · 关键词「${query}」` : ''}
        </span>
        {recordingsQuery.isFetching && <span className="library-result-meta__fetching">更新中…</span>}
      </div>

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
          icon={<InboxOutlined />}
          title={hasFilter ? '没有匹配的录音' : isAdmin ? '还没有任何录音' : '还没有你自己的录音'}
          description={
            hasFilter
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
                  setSearchParams(new URLSearchParams(), { replace: true })
                }}
              >
                清除筛选
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
            <RecordingCard key={recording.id} recording={recording} showOwner={isAdmin} />
          ))}
        </div>
      ) : (
        <div className="recording-list">
          {items.map((recording) => (
            <RecordingRow key={recording.id} recording={recording} showOwner={isAdmin} />
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
    </div>
  )
}
