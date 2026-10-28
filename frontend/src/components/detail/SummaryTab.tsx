import { Alert, Button, Tooltip } from 'antd'
import { ReloadOutlined, RobotOutlined, ThunderboltOutlined, BulbOutlined, CheckSquareOutlined, TagsOutlined } from '@ant-design/icons'
import type { Insights, RecordingStatus } from '../../types'
import { analysisKindLabel } from '../../theme'
import { formatDate } from '../../utils/format'
import { MarkdownView } from '../common/MarkdownView'
import { EmptyState } from '../common/EmptyState'
import { ErrorState } from '../common/ErrorState'
import { SummarySkeleton } from '../common/Skeletons'
import { ActionItemList, InsightCards, KeywordCloud } from './InsightWidgets'

interface SummaryTabProps {
  recording: { id: number; status: RecordingStatus }
  insights: Insights | undefined
  loading: boolean
  error: unknown
  regenerating: boolean
  onRegenerate: (kinds?: string[]) => void
  onOpenInsights: () => void
  onSeek: (ms: number) => void
  onRetry: () => void
  /** Public share page mode: hide all generate/regenerate affordances. */
  readOnly?: boolean
}

function MissingBox({
  message,
  onGenerate,
  loading,
  readOnly = false,
}: {
  message: string
  onGenerate: () => void
  loading: boolean
  readOnly?: boolean
}) {
  return (
    <div className="missing-box">
      <span>{message}</span>
      {!readOnly && (
        <Button size="small" type="primary" ghost icon={<ThunderboltOutlined />} loading={loading} onClick={onGenerate}>
          立即生成
        </Button>
      )}
    </div>
  )
}

export function SummaryTab({
  recording,
  insights,
  loading,
  error,
  regenerating,
  onRegenerate,
  onOpenInsights,
  onSeek,
  onRetry,
  readOnly = false,
}: SummaryTabProps) {
  if (loading) return <SummarySkeleton />
  if (error) {
    return <ErrorState title="AI 纪要加载失败" description="请检查网络连接后重试。" onRetry={onRetry} />
  }
  if (!insights) {
    return (
      <EmptyState
        icon={<RobotOutlined />}
        title="AI 纪要尚未生成"
        description="录音处理完成后，将自动生成会议纪要、关键洞察与待办事项。"
        actions={
          readOnly ? undefined : (
            <Button type="primary" loading={regenerating} icon={<ThunderboltOutlined />} onClick={() => onRegenerate()}>
              生成 AI 纪要
            </Button>
          )
        }
      />
    )
  }

  const missing = new Set(insights.missing ?? [])
  const missingContentKinds = (insights.missing ?? []).filter((kind) =>
    ['summary', 'chapters', 'viewpoint_tree', 'insights', 'action_items', 'keywords'].includes(kind),
  )
  const hasAny = Boolean(
    insights.summary || insights.insights?.length || insights.action_items?.length || insights.keywords?.length,
  )

  return (
    <div className="summary-tab">
      {missingContentKinds.length > 0 && (
        <Alert
          className="analysis-warning"
          type="warning"
          showIcon
          message="部分 AI 内容未生成"
          description={`缺少：${missingContentKinds.map((kind) => analysisKindLabel[kind] ?? kind).join('、')}。这是模型输出格式不完整时的自动降级，不影响录音与转录；可以重试 AI 分析。`}
          action={
            readOnly ? undefined : (
              <Button size="small" loading={regenerating} onClick={() => onRegenerate()}>
                重新生成
              </Button>
            )
          }
        />
      )}

      <section className="summary-card">
        <header className="summary-card__head">
          <div className="summary-card__title">
            <span className="summary-card__icon">
              <RobotOutlined />
            </span>
            <div>
              <h3>AI 纪要</h3>
              <div className="summary-card__meta">
                {insights.generated_at ? `生成于 ${formatDate(insights.generated_at)}` : '尚未生成'}
                {insights.model ? ` · ${insights.model}` : ''}
              </div>
            </div>
          </div>
          <div className="summary-card__actions">
            {!readOnly && recording.status === 'ready' && (
              <Tooltip title="重新生成全部 AI 内容">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={regenerating}
                  onClick={() => onRegenerate()}
                >
                  重新生成
                </Button>
              </Tooltip>
            )}
          </div>
        </header>

        {insights.summary ? (
          <MarkdownView content={insights.summary.markdown} onTimestampClick={onSeek} className="summary-markdown" />
        ) : (
          <MissingBox
            message={`${analysisKindLabel.summary}${missing.has('summary') ? '尚未生成' : '暂无内容'}`}
            loading={regenerating}
            readOnly={readOnly}
            onGenerate={() => onRegenerate(['summary'])}
          />
        )}
      </section>

      {!hasAny && missingContentKinds.length > 0 && (
        <div className="summary-empty-all">
          <EmptyState
            icon={<ThunderboltOutlined />}
            title="AI 分析结果尚未就绪"
            description={`缺少：${missingContentKinds.map((kind) => analysisKindLabel[kind] ?? kind).join('、')}`}
            actions={
              readOnly ? undefined : (
                <Button type="primary" loading={regenerating} onClick={() => onRegenerate()}>
                  生成本场纪要
                </Button>
              )
            }
          />
        </div>
      )}

      <section className="summary-section">
        <header className="summary-section__head">
          <div className="summary-section__title">
            <BulbOutlined className="summary-section__icon summary-section__icon--purple" />
            <h3>关键洞察</h3>
            {insights.insights?.length ? <span className="summary-section__count">{insights.insights.length}</span> : null}
          </div>
          {insights.insights?.length ? (
            <Button type="link" size="small" onClick={onOpenInsights}>
              查看全部
            </Button>
          ) : null}
        </header>
        {insights.insights?.length ? (
          <InsightCards insights={insights.insights} />
        ) : (
          <MissingBox
            message="关键洞察尚未生成"
            loading={regenerating}
            readOnly={readOnly}
            onGenerate={() => onRegenerate(['insights'])}
          />
        )}
      </section>

      <section className="summary-section">
        <header className="summary-section__head">
          <div className="summary-section__title">
            <CheckSquareOutlined className="summary-section__icon summary-section__icon--green" />
            <h3>待办事项</h3>
          </div>
        </header>
        <ActionItemList recordingId={recording.id} items={insights.action_items} />
      </section>

      {insights.keywords?.length ? (
        <section className="summary-section">
          <header className="summary-section__head">
            <div className="summary-section__title">
              <TagsOutlined className="summary-section__icon summary-section__icon--blue" />
              <h3>关键词</h3>
            </div>
          </header>
          <KeywordCloud keywords={insights.keywords} />
        </section>
      ) : null}
    </div>
  )
}
