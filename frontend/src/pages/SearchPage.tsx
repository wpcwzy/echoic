import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Button, Input, Tag, Tooltip } from 'antd'
import {
  ArrowRightOutlined,
  ClockCircleOutlined,
  FileSearchOutlined,
  SearchOutlined,
  SoundOutlined,
  TagsOutlined,
} from '@ant-design/icons'
import { useRecordings, useSearch, useTags } from '../api/queries'
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

export default function SearchPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const query = searchParams.get('q') ?? ''
  const [input, setInput] = useState(query)
  const searchQuery = useSearch(query)
  const tagsQuery = useTags()
  const recentQuery = useRecordings({ page_size: 6, sort: 'recent' }, !query)

  useEffect(() => {
    document.title = query ? `搜索「${query}」 · Echoic` : '全局搜索 · Echoic'
  }, [query])

  useEffect(() => {
    setInput(query)
  }, [query])

  const submit = (value: string) => {
    const next = value.trim()
    const params = new URLSearchParams(searchParams)
    if (next) params.set('q', next)
    else params.delete('q')
    setSearchParams(params, { replace: true })
  }

  return (
    <div className="search-page">
      <header className="search-header">
        <h1 className="page-header__title">全局搜索</h1>
        <p className="page-header__subtitle">同时搜索录音标题、描述与全部转录片段</p>
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
      ) : searchQuery.isError ? (
        <ErrorState title="搜索失败" description="请稍后重试。" onRetry={() => void searchQuery.refetch()} />
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
