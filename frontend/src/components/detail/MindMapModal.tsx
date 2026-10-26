import { useMemo, useRef } from 'react'
import { Modal } from 'antd'
import { ApartmentOutlined } from '@ant-design/icons'
import { Background, Controls, ReactFlow, type NodeTypes } from '@xyflow/react'
import { useResolvedTheme } from '../../store/theme'
import type { ViewpointNode } from '../../types'
import { buildMindMap, kindColor, type MindNodeData } from '../../utils/mindmap'
import { MindMapNodeView } from './MindMapNode'
import { EmptyState } from '../common/EmptyState'

const nodeTypes = { mind: MindMapNodeView } as unknown as NodeTypes

const LEGEND = [
  { kind: 'topic', label: '主题' },
  { kind: 'point', label: '观点' },
  { kind: 'task', label: '待办' },
  { kind: 'risk', label: '风险' },
]

/** Below this scale labels stop being legible — prefer a readable initial view. */
const MIN_READABLE_ZOOM = 0.55

interface MindMapModalProps {
  open: boolean
  onClose: () => void
  root: ViewpointNode | null
  title?: string
  onSeek: (ms: number) => void
}

/**
 * Full-size mind map modal: dagre layout + React Flow canvas.
 * Clicking a node seeks the audio to its `time_ms` when available.
 */
export function MindMapModal({ open, onClose, root, title, onSeek }: MindMapModalProps) {
  const resolvedTheme = useResolvedTheme()
  const gridColor = resolvedTheme === 'dark' ? '#262b33' : '#e6e9ef'
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const graph = useMemo(() => (root ? buildMindMap(root) : { nodes: [], edges: [] }), [root])
  const bounds = useMemo(() => {
    if (!graph.nodes.length) return { width: 1, height: 1 }
    let minX = Number.POSITIVE_INFINITY
    let minY = Number.POSITIVE_INFINITY
    let maxX = Number.NEGATIVE_INFINITY
    let maxY = Number.NEGATIVE_INFINITY
    graph.nodes.forEach((node) => {
      const width = Number(node.style?.width) || 200
      const height = Number(node.style?.height) || 52
      minX = Math.min(minX, node.position.x)
      minY = Math.min(minY, node.position.y)
      maxX = Math.max(maxX, node.position.x + width)
      maxY = Math.max(maxY, node.position.y + height)
    })
    return { width: maxX - minX, height: maxY - minY }
  }, [graph.nodes])
  const rootId = graph.nodes[0]?.id

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="min(1480px, calc(100vw - 96px))"
      className="mindmap-modal"
      destroyOnClose
      style={{ top: 24 }}
      title={
        <div className="mindmap-title">
          <span className="mindmap-title__main">
            <ApartmentOutlined /> 思维导图
          </span>
          {title && <span className="mindmap-title__sub">{title}</span>}
        </div>
      }
      styles={{ body: { padding: 0 } }}
    >
      {!root ? (
        <div className="mindmap-empty">
          <EmptyState
            variant="plain"
            icon={<ApartmentOutlined />}
            title="思维导图尚未生成"
            description="AI 分析完成后，可以在这里以导图形式回顾整场会议的脉络。"
          />
        </div>
      ) : (
        <>
          <div className="mindmap-toolbar">
            <div className="mindmap-legend">
              {LEGEND.map((item) => (
                <span className="mindmap-legend__item" key={item.kind}>
                  <span className="mindmap-legend__dot" style={{ background: kindColor(item.kind) }} />
                  {item.label}
                </span>
              ))}
            </div>
            <span className="mindmap-hint">
              共 {graph.nodes.length} 个节点 · 滚轮缩放 · 拖拽平移 · 点击带时间的节点可跳转播放
            </span>
          </div>
          <div className="mindmap-canvas" ref={canvasRef}>
            <ReactFlow
              nodes={graph.nodes}
              edges={graph.edges}
              nodeTypes={nodeTypes}
              minZoom={0.15}
              maxZoom={2}
              nodesDraggable={false}
              nodesConnectable={false}
              edgesFocusable={false}
              elementsSelectable={false}
              onNodeClick={(_, node) => {
                const timeMs = (node.data as MindNodeData).timeMs
                if (typeof timeMs === 'number') onSeek(timeMs)
              }}
              onInit={(instance) => {
                // Fit the graph, but never zoom out below a readable scale.
                // Large trees open centred on the root node; the fit-view control
                // in the corner still lets users see the whole map.
                window.setTimeout(() => {
                  void (async () => {
                    try {
                      await instance.fitView({ padding: 0.12, maxZoom: 1 })
                      if (instance.getZoom() >= MIN_READABLE_ZOOM) return
                      const canvas = canvasRef.current
                      const canvasWidth = canvas?.clientWidth ?? 1200
                      const canvasHeight = canvas?.clientHeight ?? 640
                      const zoom = Math.max(
                        0.6,
                        Math.min(0.9, (canvasWidth - 120) / Math.max(1, bounds.width)),
                      )
                      const rootNode = rootId ? instance.getNode(rootId) : undefined
                      const rootX = rootNode?.position.x ?? 24
                      const rootY = rootNode?.position.y ?? 24
                      const rootHeight =
                        rootNode?.measured?.height ?? Number(rootNode?.style?.height) ?? 52
                      instance.setViewport({
                        x: 56 - rootX * zoom,
                        y: canvasHeight / 2 - (rootY + rootHeight / 2) * zoom,
                        zoom,
                      })
                    } catch {
                      /* canvas measured late — keep default viewport */
                    }
                  })()
                }, 60)
              }}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={20} color={gridColor} />
              <Controls showInteractive={false} position="bottom-right" />
            </ReactFlow>
          </div>
        </>
      )}
    </Modal>
  )
}
