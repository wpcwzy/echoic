import type { ViewpointNode, ViewpointTree } from '../types'

/**
 * Normalize the stored viewpoint payload into a forest of topic trees.
 *
 * - v0.3.4 payloads already contain `roots` (one tree per meeting topic).
 * - Legacy payloads contain a single `root` whose children are the meeting's
 *   topic branches; those children are promoted to forest roots so older
 *   recordings render the same way as new ones.
 */
export function viewpointForest(tree?: ViewpointTree | null): ViewpointNode[] {
  if (!tree) return []
  if (tree.roots?.length) return tree.roots
  const root = tree.root
  if (!root) return []
  const children = root.children ?? []
  if (children.length >= 2) return children
  return [root]
}

export function countViewpointNodes(nodes: ViewpointNode[]): number {
  return nodes.reduce((total, node) => total + 1 + countViewpointNodes(node.children ?? []), 0)
}

/** Wrap a forest into a single synthetic root for React Flow / mind map layouts. */
export function viewpointMindMapRoot(
  nodes: ViewpointNode[],
  title?: string,
): ViewpointNode | null {
  if (!nodes.length) return null
  if (nodes.length === 1) return nodes[0]
  return {
    id: 'forest-root',
    label: title || '会议专题全景',
    detail: `${nodes.length} 个专题`,
    kind: 'topic',
    children: nodes,
  }
}
