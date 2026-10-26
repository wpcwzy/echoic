import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Button, Input, Segmented, Tag, Tooltip } from 'antd'
import {
  ArrowRightOutlined,
  ClockCircleOutlined,
  FileSearchOutlined,
  ReloadOutlined,
  SearchOutlined,
  SoundOutlined,
  TagsOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useRecordings, useSearch, useSystemFeatures, useTags } from '../api/queries'
import { useVectorSearch } from '../api/vector'
import type { SearchMode } from '../types'
import { formatDateShort, formatMs, splitHighlight } from '../utils/format'
import { EmptyState } from '../components/common/EmptyState'
import { ErrorState } from '../components/common/ErrorState'
import { RecordingListSkeleton } from '../components/common/Skeletons'
import { StatusBadge } from '../components/common/StatusBadge'

function Highlighted({ text, query }: { text: string; query: string }) {
  const parts = splitHighlight(text, query)
  return (
    <>
      {parts.map((part, index) => (part.hit ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>))}
    </>
  )
}

const MODE_OPTIONS = [
  { label: '关键词', value: 'keyword' },
  { label: '混合检索', value: 'hybrid' },
  { label: '语义检索', value: 'semantic' },
]

function parseMode(value: string | null): SearchMode {
  return value === 'hybrid' || value === 'semantic' ? value : 'keyword'
}

export default function SearchPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const query = searchParams.get('q') ?? ''
  const mode = parseMode(searchParams.get('mode'))
  const [input, setInput] = useState(query)
  const searchQuery = useSearch(query)
  const tagsQuery = useTags()
  const featuresQuery = useSystemFeatures()
  const recentQuery = useRecordings({ page_size: 6, sort: 'recent' }, !query)

  const vectorFeatures = featuresQuery.data?.vector
  const vectorAvailable = Boolean(vectorFeatures?.search_enabled && vectorFeatures?.embeddings_configured)
  const vectorDisabledReason = !vectorFeatures
    ? '正在检测语义检索能力…'
    : !vectorFeatures.search_enabled
      ? '服务端已关闭语义检索'
      : '管理员尚未配置 Embedding 模型，可在「设置 → AI 服务」中配置并重建索引'

  const vectorQuery = useVectorSearch(query, mode === 'keyword' ? 'hybrid' : mode, Boolean(query) && vectorAvailable && mode !== 'keyword')

  useEffect(() => {
    document.title = query ? `搜索「${query}」 · Echoic` : '全局搜索 · Echoic'
  }, [query])

  useEffect(() => {
    setInput(query)
  }, [query])

  useEffect(() => {
    // If semantic/hybrid is unavailable (e.g. feature flag off), fall back cleanly.
    if (mode !== 'keyword' && featuresQuery.isSuccess && !vectorAvailable) {
      const params = new URLSearchParams(searchParams)
      params.delete('mode')
      setSearchParams(params, { replace: true })
    }
  }, [featuresQuery.isSuccess, mode, searchParams, setSearchParams, vectorAvailable])

  const submit = (value: string) => {
    const next = value.trim()
    const params = new URLSearchParams(searchParams)
    if (next) params.set('q', next)
    else params.delete('q')
    setSearchParams(params, { replace: true })
  }

  const changeMode = (nextMode: SearchMode) => {
    const params = new URLSearchParams(searchParams)
    if (nextMode === 'keyword') params.delete('mode')
    else params.set('mode', nextMode)
    setSearchParams(params, { replace: true })
  }

  const modeOptions = useMemo(
    () =>
      MODE_OPTIONS.map((option) => ({
        ...option,
        disabled: option.value !== 'keyword' && !vectorAvailable,
      })),
    [vectorAvailable],
  )

  const vectorResults = vectorQuery.data?.results ?? []
  const vectorFallback = vectorQuery.data?.fallback_reason ?? null

  return (
    <div className="search-page">
      <header className="search-header">
        <h1 className="page-header__title">全局搜索</h1>
        <p className="page-header__subtitle">关键词检索录音标题、描述与转录；也可使用语义/混合检索按意思找内容</p>
        <Input.Search
          className="search-page__input"
          size="large"
          allowClear
          autoFocus
          value={input}
          prefix={<SearchOutlined className="muted-icon" />}
          placeholder="搜索录音标题、转录内容，例如「论文分工」"
          enterButton="搜索"
          onChange={(event) => setInput(event.target.value)}
          onSearch={submit}
        />
        <div className="search-mode-row">
          <Segmented
            value={mode}
            options={modeOptions}
            onChange={(value) => changeMode(value as SearchMode)}
          />
          {mode !== 'keyword' && vectorFeatures ? (
            <span className="search-mode-hint">
              <ThunderboltOutlined /> 向量后端：{vectorFeatures.backend}
              {vectorFeatures.embeddings_configured ? '' : ' · 未配置模型'}
            </span>
          ) : (
            <Tooltip title={vectorAvailable ? `向量后端：${vectorFeatures?.backend ?? 'numpy'}` : vectorDisabledReason}>
              <span className="search-mode-hint">{vectorAvailable ? '语义检索已就绪' : '语义检索不可用'}</span>
            </Tooltip>
          )}
        </div>
        {!vectorAvailable && featuresQuery.isSuccess && (
          <Alert
            className="search-mode-alert"
            type="info"
            showIcon
            message="语义检索暂不可用，当前使用关键词检索"
            description={vectorDisabledReason}
          />
        )}
      </header>

      {!query ? (
        <div className="search-discovery">
          <section className="search-discovery__section">
            <h2>
              <TagsOutlined /> 按标签浏览
            </h2>
            {tagsQuery.data?.items?.length ? (
              <div className="search-tag-cloud">
                {tagsQuery.data.items.map((tag) => (
                  <Tag
                    key={tag.id}
                    className="search-tag"
                    onClick={() => submit(tag.name)}
                    style={{ cursor: 'pointer', color: tag.color, borderColor: `${tag.color}55` }}
                  >
                    {tag.name} · {tag.recording_count}
                  </Tag>
                ))}
              </div>
            ) : (
              <div className="muted-text">还没有创建标签，可以在录音详情或资料库中管理。</div>
            )}
          </section>

          <section className="search-discovery__section">
            <h2>
              <SoundOutlined /> 最近的录音
            </h2>
            {recentQuery.isLoading ? (
              <RecordingListSkeleton count={3} />
            ) : recentQuery.data?.items?.length ? (
              <div className="search-recent-list">
                {recentQuery.data.items.map((recording) => (
                  <Link to={`/r/${recording.id}`} className="search-recent-row" key={recording.id}>
                    <span className="search-recent-row__title">{recording.title}</span>
                    <StatusBadge status={recording.status} size="small" />
                    <span className="search-recent-row__meta">{formatDateShort(recording.created_at)}</span>
                    <span className="search-recent-row__duration">{formatMs(recording.duration_ms)}</span>
                    <ArrowRightOutlined className="search-recent-row__arrow" />
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState variant="plain" compact title="暂无录音" description="上传录音后就可以在这里搜索内容。" />
            )}
          </section>
        </div>
      ) : mode !== 'keyword' ? (
        <>
          {vectorFallback && (
            <Alert
              className="search-fallback-alert"
              type="warning"
              showIcon
              message="已自动降级为关键词检索"
              description={vectorFallback}
            />
          )}
          {vectorQuery.isError ? (
            <ErrorState
              title="语义检索失败"
              description={vectorQuery.error?.message || '请稍后重试。'}
              onRetry={() => void vectorQuery.refetch()}
            />
          ) : vectorQuery.isLoading ? (
            <RecordingListSkeleton count={5} />
          ) : vectorResults.length === 0 ? (
            <EmptyState
              icon={<FileSearchOutlined />}
              title={`没有找到与「${query}」语义相关的内容`}
              description="换一种说法，或切换到关键词检索再试一次。"
              actions={
                <Button onClick={() => changeMode('keyword')}>切换到关键词检索</Button>
              }
            />
          ) : (
            <div className="vector-results">
              <div className="vector-results__meta">
                <span>
                  {mode === 'semantic' ? '语义检索' : '混合检索'} · 找到 {vectorResults.length} 条结果
                </span>
                <span className="vector-results__backend">score 越高越相关</span>
              </div>
              {vectorResults.map((hit) => (
                <button
                  type="button"
                  className="vector-result"
                  key={`${hit.recording_id}-${hit.segment_id ?? hit.start_ms}`}
                  onClick={() =>
                    navigate(`/r/${hit.recording_id}?${hit.segment_id ? `segment=${hit.segment_id}&` : ''}t=${hit.start_ms}`)
                  }
                >
                  <div className="vector-result__head">
                    <span className="vector-result__title">{hit.recording_title}</span>
                    {hit.speaker && <span className="vector-result__speaker">{hit.speaker}</span>}
                    <span className="vector-result__time">
                      <ClockCircleOutlined /> {formatMs(hit.start_ms)}
                    </span>
                    <span className="vector-result__score">相关度 {Math.round(hit.score * 100)}%</span>
                  </div>
                  <p className="vector-result__text">
                    <Highlighted text={hit.text} query={query} />
                  </p>
                </button>
              ))}
            </div>
          )}
        </>
      ) : searchQuery.isError ? (
        <ErrorState
          title="搜索失败"
          description="请稍后重试。"
          onRetry={() => void searchQuery.refetch()}
        />
      ) : searchQuery.isLoading ? (
        <RecordingListSkeleton count={5} />
      ) : !searchQuery.data || (!searchQuery.data.recordings.length && !searchQuery.data.segments.length) ? (
        <EmptyState
          icon={<FileSearchOutlined />}
          title={`没有找到与「${query}」相关的内容`}
          description="试试更换关键词，或搜索说话人名字、会议主题。"
          actions={
            <Button
              onClick={() => {
                setInput('')
                submit('')
              }}
            >
              重新搜索
            </Button>
          }
        />
      ) : (
        <div className="search-results">
          {searchQuery.data.recordings.length > 0 && (
            <section className="search-group">
              <header className="search-group__head">
                <h2>录音</h2>
                <span>{searchQuery.data.recordings.length} 条</span>
              </header>
              <div className="search-group__list">
                {searchQuery.data.recordings.map((recording) => (
                  <button
                    type="button"
                    className="search-result-recording"
                    key={recording.id}
                    onClick={() => navigate(`/r/${recording.id}`)}
                  >
                    <span className="search-result-recording__icon">
                      <SoundOutlined />
                    </span>
                    <span className="search-result-recording__body">
                      <span className="search-result-recording__title">
                        <Highlighted text={recording.title} query={query} />
                      </span>
                      {recording.description && (
                        <span className="search-result-recording__desc">
                          <Highlighted text={recording.description} query={query} />
                        </span>
                      )}
                      <span className="search-result-recording__meta">
                        {formatDateShort(recording.created_at)} · {formatMs(recording.duration_ms)} · 命中{' '}
                        {recording.hit_count} 处
                      </span>
                    </span>
                    <StatusBadge status={recording.status} size="small" />
                    <ArrowRightOutlined className="search-result-recording__arrow" />
                  </button>
                ))}
              </div>
            </section>
          )}

          {searchQuery.data.segments.length > 0 && (
            <section className="search-group">
              <header className="search-group__head">
                <h2>转录片段</h2>
                <span>{searchQuery.data.segments.length} 条</span>
              </header>
              <div className="search-group__list">
                {searchQuery.data.segments.map((segment) => (
                  <button
                    type="button"
                    className="search-result-segment"
                    key={`${segment.recording_id}-${segment.segment_id}`}
                    onClick={() =>
                      navigate(`/r/${segment.recording_id}?segment=${segment.segment_id}&t=${segment.start_ms}`)
                    }
                  >
                    <div className="search-result-segment__head">
                      <span className="search-result-segment__recording">{segment.recording_title}</span>
                      {segment.speaker && <span className="search-result-segment__speaker">{segment.speaker}</span>}
                      <Tooltip title="跳转到该片段">
                        <span className="search-result-segment__time">
                          <ClockCircleOutlined /> {formatMs(segment.start_ms)}
                        </span>
                      </Tooltip>
                    </div>
                    <p className="search-result-segment__snippet">
                      <Highlighted text={segment.snippet} query={query} />
                    </p>
                  </button>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  )
}
