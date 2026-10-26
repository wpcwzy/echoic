import * as dagreNamespace from 'dagre'
import { Position, type Edge, type Node } from '@xyflow/react'
import type { ViewpointNode } from '../types'

// dagre is CommonJS: depending on the bundler interop the API may live on the
// namespace itself or under `default`. Normalise both cases at runtime.
type DagreModule = typeof dagreNamespace
const dagre: DagreModule =
  (dagreNamespace as unknown as { default?: DagreModule }).default ?? dagreNamespace

export interface MindNodeData extends Record<string, unknown> {
  label: string
  detail?: string
  kind?: string
  timeMs?: number | null
  speaker?: string | null
  depth: number
}

export type MindFlowNode = Node<MindNodeData, 'mind'>

export interface MindMapGraph {
  nodes: MindFlowNode[]
  edges: Edge[]
}

const KIND_COLORS: Record<string, string> = {
  topic: '#1677ff',
  point: '#52c41a',
  task: '#faad14',
  risk: '#ff4d4f',
}

export function kindColor(kind?: string): string {
  return KIND_COLORS[kind ?? 'point'] ?? KIND_COLORS.point
}

function measureNode(node: ViewpointNode): { width: number; height: number } {
  const labelLength = (node.label || '').length
  const width = Math.max(150, Math.min(272, Math.round(labelLength * 14 + 46)))
  const base = node.detail ? 68 : 46
  const meta = typeof node.time_ms === 'number' || node.speaker ? 17 : 0
  return { width, height: base + meta }
}

/**
 * Flattens the viewpoint tree into React Flow nodes/edges and lays them out
 * left-to-right with dagre.
 */
export function buildMindMap(root: ViewpointNode): MindMapGraph {
  const nodes: MindFlowNode[] = []
  const edges: Edge[] = []
  const dimensions = new Map<string, { width: number; height: number }>()
  let sequence = 0

  const walk = (node: ViewpointNode, parentId: string | null, depth: number): void => {
    const id = `mm-${sequence++}`
    const size = measureNode(node)
    dimensions.set(id, size)
    nodes.push({
      id,
      type: 'mind',
      position: { x: 0, y: 0 },
      data: {
        label: node.label,
        detail: node.detail,
        kind: node.kind ?? (depth === 0 ? 'topic' : 'point'),
        timeMs: typeof node.time_ms === 'number' ? node.time_ms : null,
        speaker: node.speaker ?? null,
        depth,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      style: { width: size.width, height: size.height },
    })
    if (parentId) {
      edges.push({
        id: `edge-${parentId}-${id}`,
        source: parentId,
        target: id,
        type: 'smoothstep',
        style: { stroke: '#cfd8e6', strokeWidth: 1.5 },
      })
    }
    ;(node.children ?? []).forEach((child) => walk(child, id, depth + 1))
  }

  walk(root, null, 0)

  const graph = new dagre.graphlib.Graph()
  graph.setDefaultEdgeLabel(() => ({}))
  graph.setGraph({
    rankdir: 'LR',
    nodesep: 14,
    ranksep: 56,
    marginx: 24,
    marginy: 24,
  })

  nodes.forEach((node) => {
    const size = dimensions.get(node.id)
    if (size) graph.setNode(node.id, size)
  })
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target))
  dagre.layout(graph)

  nodes.forEach((node) => {
    const size = dimensions.get(node.id)
    const point = graph.node(node.id)
    if (!size || !point) return
    node.position = { x: point.x - size.width / 2, y: point.y - size.height / 2 }
  })

  return { nodes, edges }
}
