import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button, Tooltip } from 'antd'
import {
  ArrowRightOutlined,
  CloudUploadOutlined,
  InboxOutlined,
  ReloadOutlined,
  SoundOutlined,
} from '@ant-design/icons'
import { useRecordings } from '../api/queries'
import { formatMs, formatRelative } from '../utils/format'
import { DemoModeBanner } from '../components/common/DemoModeBanner'
import { EmptyState } from '../components/common/EmptyState'
import { StatusBadge } from '../components/common/StatusBadge'
import { RecordingListSkeleton } from '../components/common/Skeletons'
import { FileUploader } from '../components/common/FileUploader'
import { RecorderPanel } from '../components/record/RecorderPanel'

export default function RecordPage() {
  const navigate = useNavigate()
  const recentQuery = useRecordings({ page_size: 8, sort: 'recent' })

  useEffect(() => {
    document.title = '录音与上传 · Echoic'
  }, [])

  const items = recentQuery.data?.items ?? []

  return (
    <div className="record-page">
      <DemoModeBanner />

      <header className="page-header">
        <div>
          <h1 className="page-header__title">录音与上传</h1>
          <p className="page-header__subtitle">使用浏览器直接录音，或上传已有的会议音频与视频文件</p>
        </div>
        <Button icon={<InboxOutlined />} onClick={() => navigate('/')}>
          前往资料库
          <ArrowRightOutlined />
        </Button>
      </header>

      <div className="record-grid">
        <RecorderPanel
          onUploaded={() => {
            void recentQuery.refetch()
          }}
        />

        <section className="upload-panel">
          <header className="recorder-panel__head">
            <div className="recorder-panel__title">
              <span className="recorder-panel__title-icon recorder-panel__title-icon--purple">
                <CloudUploadOutlined />
              </span>
              <div>
                <h2>上传音频 / 视频</h2>
                <p>支持 mp3、wav、m4a、mp4 等常见格式，可批量上传</p>
              </div>
            </div>
          </header>
          <FileUploader
            onUploaded={() => {
              void recentQuery.refetch()
            }}
          />
          <div className="upload-panel__tips">
            <div className="upload-panel__tip">
              <SoundOutlined /> 长录音上传后会在后台自动分段转写
            </div>
            <div className="upload-panel__tip">
              <InboxOutlined /> 视频文件会自动提取音轨进行分析
            </div>
          </div>
        </section>
      </div>

      <section className="recent-uploads">
        <header className="recent-uploads__head">
          <div>
            <h2>最近上传</h2>
            <p>自动刷新处理进度，点击可查看转写与 AI 纪要</p>
          </div>
          <div className="recent-uploads__actions">
            <Tooltip title="刷新">
              <Button
                type="text"
                icon={<ReloadOutlined />}
                aria-label="刷新"
                loading={recentQuery.isFetching}
                onClick={() => void recentQuery.refetch()}
              />
            </Tooltip>
            <Link to="/">
              <Button type="link">查看全部</Button>
            </Link>
          </div>
        </header>

        {recentQuery.isLoading ? (
          <RecordingListSkeleton count={4} />
        ) : items.length === 0 ? (
          <EmptyState
            variant="plain"
            compact
            icon={<InboxOutlined />}
            title="还没有上传记录"
            description="上传或录制第一段录音后，会在这里显示处理进度。"
          />
        ) : (
          <div className="recent-upload-list">
            {items.map((recording) => (
              <Link to={`/r/${recording.id}`} className="recent-upload-row" key={recording.id}>
                <span className="recent-upload-row__icon">
                  <SoundOutlined />
                </span>
                <span className="recent-upload-row__title">{recording.title || '未命名录音'}</span>
                <span className="recent-upload-row__meta">
                  {formatRelative(recording.created_at)} · {formatMs(recording.duration_ms)}
                </span>
                <StatusBadge status={recording.status} progress={recording.progress} stage={recording.stage} size="small" />
                <ArrowRightOutlined className="recent-upload-row__arrow" />
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
