import { Modal } from 'antd'
import { BulbOutlined, CheckSquareOutlined, TagsOutlined } from '@ant-design/icons'
import type { Insights } from '../../types'
import { EmptyState } from '../common/EmptyState'
import { ActionItemList, InsightCards, KeywordCloud } from './InsightWidgets'

interface InsightsModalProps {
  open: boolean
  onClose: () => void
  insights: Insights | undefined
  recordingId: number
}

/** Full 关键洞察 + 待办 list modal, opened from the AI panel. */
export function InsightsModal({ open, onClose, insights, recordingId }: InsightsModalProps) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      className="insights-modal"
      title={
        <span className="insights-modal__title">
          <BulbOutlined /> 关键洞察
        </span>
      }
    >
      {!insights ? (
        <EmptyState variant="plain" title="AI 分析尚未就绪" description="录音处理完成后即可查看关键洞察。" />
      ) : (
        <div className="insights-modal__body">
          <section className="insights-modal__section">
            {insights.insights?.length ? (
              <InsightCards insights={insights.insights} />
            ) : (
              <EmptyState variant="plain" compact title="暂无关键洞察" description="可以尝试在 AI 纪要中重新生成分析结果。" />
            )}
          </section>

          <section className="insights-modal__section">
            <h4 className="insights-modal__heading">
              <CheckSquareOutlined /> 待办事项
            </h4>
            <ActionItemList recordingId={recordingId} items={insights.action_items} />
          </section>

          {insights.keywords?.length ? (
            <section className="insights-modal__section">
              <h4 className="insights-modal__heading">
                <TagsOutlined /> 关键词
              </h4>
              <KeywordCloud keywords={insights.keywords} />
            </section>
          ) : null}
        </div>
      )}
    </Modal>
  )
}
