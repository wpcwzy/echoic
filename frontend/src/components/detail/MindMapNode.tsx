import { ClockCircleOutlined } from '@ant-design/icons'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { formatMs } from '../../utils/format'
import type { MindFlowNode } from '../../utils/mindmap'

const KIND_LABEL: Record<string, string> = {
  topic: '主题',
  point: '观点',
  task: '待办',
  risk: '风险',
}

/** Custom React Flow node used by the 思维导图 modal. */
export function MindMapNodeView({ data }: NodeProps<MindFlowNode>) {
  const kind = data.kind ?? 'point'
  return (
    <div className={`mind-node mind-node--${kind} mind-node--depth-${Math.min(data.depth ?? 0, 3)}`}>
      <Handle type="target" position={Position.Left} className="mind-handle" isConnectable={false} />
      <div className="mind-node__label">{data.label}</div>
      {data.detail && <div className="mind-node__detail">{data.detail}</div>}
      <div className="mind-node__meta">
        {typeof data.timeMs === 'number' && (
          <span className="mind-node__time">
            <ClockCircleOutlined /> {formatMs(data.timeMs)}
          </span>
        )}
        {data.speaker && <span className="mind-node__speaker">{data.speaker}</span>}
        <span className={`mind-node__kind mind-node__kind--${kind}`}>{KIND_LABEL[kind] ?? '观点'}</span>
      </div>
      <Handle type="source" position={Position.Right} className="mind-handle" isConnectable={false} />
    </div>
  )
}
