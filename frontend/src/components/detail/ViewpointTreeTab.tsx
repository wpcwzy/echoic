import { useMemo } from 'react'
import { Button } from 'antd'
import { ClockCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import type { Insights, ViewpointNode } from '../../types'
import { formatMs } from '../../utils/format'
import { EmptyState } from '../common/EmptyState'
import { ErrorState } from '../common/ErrorState'
import { TabContentSkeleton } from '../common/Skeletons'

interface ViewpointTreeTabProps {
  insights: Insights | undefined
  loading: boolean
  error: unknown
  regenerating: boolean
  onSeek: (ms: number) => void
  onRegenerate: () => void
  onRetry: () => void
  /** Public share page mode: hide regenerate affordances. */
  readOnly?: boolean
}

function countNodes(node: ViewpointNode | null | undefined): number {
  if (!node) return 0
  return 1 + (node.children ?? []).reduce((total, child) => total + countNodes(child), 0)
}

function KindTag({ kind }: { kind?: string }) {
  if (kind === 'task') return <span className="vp-tag vp-tag--task">待办</span>
  if (kind === 'risk') return <span className="vp-tag vp-tag--risk">风险</span>
  return null
}

function NodeMeta({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  const hasTime = typeof node.time_ms === 'number' && node.time_ms !== null
  if (!hasTime && !node.speaker) return null
  return (
    <span className="vp-meta">
      {node.speaker && <span className="vp-meta__speaker">{node.speaker}</span>}
      {hasTime && (
        <button
          type="button"
          className="vp-meta__time"
          onClick={(event) => {
            event.stopPropagation()
            onSeek(node.time_ms as number)
          }}
        >
          <ClockCircleOutlined /> {formatMs(node.time_ms)}
        </button>
      )}
    </span>
  )
}

function seekProps(node: ViewpointNode, onSeek: (ms: number) => void) {
  const hasTime = typeof node.time_ms === 'number' && node.time_ms !== null
  return {
    hasTime,
    interaction: hasTime
      ? {
          role: 'button' as const,
          tabIndex: 0,
          onClick: () => onSeek(node.time_ms as number),
          onKeyDown: (event: React.KeyboardEvent) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              onSeek(node.time_ms as number)
            }
          },
        }
      : {},
  }
}

function VpBullet({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  const { interaction } = seekProps(node, onSeek)
  return (
    <li className={`vp-bullet ${typeof node.time_ms === 'number' ? 'is-clickable' : ''}`} {...interaction}>
      <span className="vp-bullet__dot" />
      <span className="vp-bullet__body">
        <span className="vp-bullet__label">
          {node.label}
          <KindTag kind={node.kind} />
          <NodeMeta node={node} onSeek={onSeek} />
        </span>
        {node.detail && <span className="vp-bullet__detail">{node.detail}</span>}
      </span>
    </li>
  )
}

function VpRow({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  const { interaction } = seekProps(node, onSeek)
  return (
    <div className={`vp-row ${typeof node.time_ms === 'number' ? 'is-clickable' : ''}`} {...interaction}>
      <span className="vp-row__dot" />
      <div className="vp-row__body">
        <div className="vp-row__head">
          <span className="vp-row__label">{node.label}</span>
          <KindTag kind={node.kind} />
          <NodeMeta node={node} onSeek={onSeek} />
        </div>
        {node.detail && <p className="vp-row__detail">{node.detail}</p>}
      </div>
    </div>
  )
}

function VpColumns({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  return (
    <div className="vp-columns">
      {(node.children ?? []).map((child) => (
        <div className="vp-column" key={child.id}>
          <div className="vp-column__head">
            {child.label}
            <KindTag kind={child.kind} />
            <NodeMeta node={child} onSeek={onSeek} />
          </div>
          {child.detail && <p className="vp-column__detail">{child.detail}</p>}
          <ul className="vp-column__list">
            {(child.children ?? []).map((leaf) => (
              <VpBullet key={leaf.id} node={leaf} onSeek={onSeek} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

function GroupCard({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  const children = node.children ?? []
  const useColumns =
    children.length >= 2 &&
    children.every((child) => child.children?.length > 0 && (child.kind === 'topic' || !child.kind))
  const { interaction, hasTime } = seekProps(node, onSeek)
  const cardClass =
    node.kind === 'task' ? 'vp-card--task' : node.kind === 'risk' ? 'vp-card--risk' : 'vp-card--group'

  return (
    <article className={`vp-card ${cardClass} ${hasTime ? 'is-clickable' : ''}`}>
      <header className="vp-card__header" {...(hasTime ? { onClick: interaction.onClick } : {})}>
        <h4 className="vp-card__title">{node.label}</h4>
        <KindTag kind={node.kind} />
        <NodeMeta node={node} onSeek={onSeek} />
      </header>
      {node.detail && <p className="vp-card__detail">{node.detail}</p>}
      {children.length > 0 && <div className="vp-card__divider" />}
      {useColumns ? (
        <VpColumns node={node} onSeek={onSeek} />
      ) : (
        <div className="vp-rows">
          {children.map((child) =>
            child.children?.length ? (
              <div className="vp-subgroup" key={child.id}>
                <div className="vp-subgroup__head">
                  {child.label}
                  <KindTag kind={child.kind} />
                  <NodeMeta node={child} onSeek={onSeek} />
                </div>
                {child.detail && <p className="vp-card__detail">{child.detail}</p>}
                <div className="vp-rows">
                  {(child.children ?? []).map((leaf) => (
                    <VpRow key={leaf.id} node={leaf} onSeek={onSeek} />
                  ))}
                </div>
              </div>
            ) : (
              <VpRow key={child.id} node={child} onSeek={onSeek} />
            ),
          )}
        </div>
      )}
    </article>
  )
}

function CalloutCard({ node, onSeek }: { node: ViewpointNode; onSeek: (ms: number) => void }) {
  const { interaction } = seekProps(node, onSeek)
  return (
    <div className={`vp-callout ${typeof node.time_ms === 'number' ? 'is-clickable' : ''}`} {...interaction}>
      <span className="vp-callout__text">{node.label}</span>
      <KindTag kind={node.kind} />
      <NodeMeta node={node} onSeek={onSeek} />
    </div>
  )
}

/**
 * 观点树 — mirrors the reference screenshot: a root topic pill connected by a
 * vertical rail to tinted group cards with bullet rows / multi-column layouts.
 */
export function ViewpointTreeTab({
  insights,
  loading,
  error,
  regenerating,
  onSeek,
  onRegenerate,
  onRetry,
  readOnly = false,
}: ViewpointTreeTabProps) {
  const root = insights?.viewpoint_tree?.root
  const total = useMemo(() => countNodes(root), [root])

  if (loading) return <TabContentSkeleton rows={4} />
  if (error) return <ErrorState title="观点树加载失败" description="AI 分析结果获取失败，请稍后重试。" onRetry={onRetry} />
  if (!root) {
    return (
      <EmptyState
        icon={<ReloadOutlined />}
        title="观点树尚未生成"
        description={
          readOnly
            ? '这份分享暂未包含 AI 观点树，或录音仍在处理中。'
            : '观点树会把会议中的主题、观点、待办与风险整理成结构化脉络，支持点击节点跳转播放。'
        }
        actions={
          readOnly ? undefined : (
            <Button type="primary" ghost loading={regenerating} icon={<ReloadOutlined />} onClick={onRegenerate}>
              生成观点树
            </Button>
          )
        }
      />
    )
  }

  const children = root.children ?? []

  return (
    <div className="viewpoint-tree">
      <div className="vp-root">
        <span
          className={`vp-root__pill ${typeof root.time_ms === 'number' ? 'is-clickable' : ''}`}
          role={typeof root.time_ms === 'number' ? 'button' : undefined}
          tabIndex={typeof root.time_ms === 'number' ? 0 : undefined}
          onClick={typeof root.time_ms === 'number' ? () => onSeek(root.time_ms as number) : undefined}
        >
          {root.label}
        </span>
        {root.detail && <span className="vp-root__detail">{root.detail}</span>}
        <span className="vp-root__count">{total} 个观点节点</span>
      </div>

      {children.length === 0 ? (
        <div className="vp-callout">
          <span className="vp-callout__text">{root.detail || '暂无更多观点内容'}</span>
        </div>
      ) : (
        <div className="vp-tree">
          {children.map((child) => (
            <div className="vp-tree__item" key={child.id}>
              {child.children?.length ? (
                <GroupCard node={child} onSeek={onSeek} />
              ) : (
                <CalloutCard node={child} onSeek={onSeek} />
              )}
            </div>
          ))}
        </div>
      )}

      <div className="vp-hint">悬停节点可查看时间与发言人，点击可跳转到对应录音位置</div>
    </div>
  )
}
