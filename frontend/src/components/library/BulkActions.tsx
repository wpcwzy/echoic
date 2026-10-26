import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Checkbox, Dropdown, Empty, Modal, Segmented, Select, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import {
  ClearOutlined,
  CloseOutlined,
  DeleteOutlined,
  ReloadOutlined,
  TagsOutlined,
  StopOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import type { BatchAction, TagWithCount } from '../../types'

interface BatchActionBarProps {
  selectedCount: number
  trashMode: boolean
  isAdmin: boolean
  processingCount: number
  busy: boolean
  onClear: () => void
  onTrash: () => void
  onRestore: () => void
  onPurge: () => void
  onTag: () => void
  onReprocess: (from: 'all' | 'asr' | 'insights') => void
  onCancelProcessing: () => void
}

/** Floating bar shown while library cards/rows are selected. */
export function BatchActionBar({
  selectedCount,
  trashMode,
  isAdmin,
  processingCount,
  busy,
  onClear,
  onTrash,
  onRestore,
  onPurge,
  onTag,
  onReprocess,
  onCancelProcessing,
}: BatchActionBarProps) {
  const reprocessItems: MenuProps['items'] = [
    { key: 'insights', label: '重新生成 AI 分析' },
    { key: 'asr', label: '重新转录（含 AI 分析）' },
    { key: 'all', label: '完整重新处理' },
  ]

  return (
    <div className="batch-bar" role="toolbar" aria-label="批量操作">
      <span className="batch-bar__count">
        已选 <strong>{selectedCount}</strong> 条
      </span>
      <span className="batch-bar__divider" />
      {trashMode ? (
        <>
          <Button type="text" icon={<UndoOutlined />} loading={busy} onClick={onRestore}>
            恢复
          </Button>
          <Button type="text" danger icon={<DeleteOutlined />} loading={busy} onClick={onPurge}>
            彻底删除
          </Button>
        </>
      ) : (
        <>
          <Button type="text" danger icon={<DeleteOutlined />} loading={busy} onClick={onTrash}>
            移入回收站
          </Button>
          {isAdmin && (
            <Button type="text" icon={<TagsOutlined />} loading={busy} onClick={onTag}>
              打标签
            </Button>
          )}
          <Dropdown
            menu={{ items: reprocessItems, onClick: ({ key }) => onReprocess(key as 'all' | 'asr' | 'insights') }}
            trigger={['click']}
            disabled={busy}
          >
            <Button type="text" icon={<ReloadOutlined />} loading={busy}>
              重跑分析
            </Button>
          </Dropdown>
          <Tooltip title={processingCount > 0 ? '取消所选录音的处理任务' : '所选录音中没有处理中的任务'}>
            <Button
              type="text"
              icon={<StopOutlined />}
              disabled={busy || processingCount === 0}
              onClick={onCancelProcessing}
            >
              取消处理
            </Button>
          </Tooltip>
        </>
      )}
      <span className="batch-bar__divider" />
      <Button type="text" icon={<ClearOutlined />} onClick={onClear}>
        取消选择
      </Button>
    </div>
  )
}

interface BatchTagDialogProps {
  open: boolean
  tags: TagWithCount[]
  loading: boolean
  onCancel: () => void
  onSubmit: (action: 'tag_add' | 'tag_remove', tagIds: number[]) => void
}

/** Admin dialog: add/remove tags for the current selection. */
export function BatchTagDialog({ open, tags, loading, onCancel, onSubmit }: BatchTagDialogProps) {
  const [mode, setMode] = useState<'tag_add' | 'tag_remove'>('tag_add')
  const [tagIds, setTagIds] = useState<number[]>([])

  useEffect(() => {
    if (open) {
      setMode('tag_add')
      setTagIds([])
    }
  }, [open])

  const options = useMemo(
    () =>
      tags.map((tag) => ({
        label: (
          <span className="batch-tag-option">
            <span className="batch-tag-option__dot" style={{ background: tag.color }} />
            {tag.name}
            <span className="batch-tag-option__count">{tag.recording_count}</span>
          </span>
        ),
        value: tag.id,
        name: tag.name,
      })),
    [tags],
  )

  return (
    <Modal
      open={open}
      title={mode === 'tag_add' ? '为所选录音添加标签' : '从所选录音移除标签'}
      okText={mode === 'tag_add' ? '添加标签' : '移除标签'}
      cancelText="取消"
      confirmLoading={loading}
      okButtonProps={{ disabled: tagIds.length === 0 }}
      onCancel={onCancel}
      onOk={() => onSubmit(mode, tagIds)}
      destroyOnClose
    >
      <div className="batch-tag-dialog">
        <Segmented
          value={mode}
          options={[
            { label: '添加', value: 'tag_add' },
            { label: '移除', value: 'tag_remove' },
          ]}
          onChange={(value) => setMode(value as 'tag_add' | 'tag_remove')}
        />
        {tags.length ? (
          <Select
            mode="multiple"
            className="batch-tag-select"
            placeholder="选择标签（可多选）"
            value={tagIds}
            options={options}
            onChange={(value) => setTagIds(value)}
            optionFilterProp="name"
            autoFocus
          />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有标签，可在录音详情页创建" />
        )}
        <div className="muted-text batch-tag-dialog__hint">
          标签批量操作仅管理员可用，作用于所选的 {tagIds.length > 0 ? '全部' : '所有'}录音。
        </div>
      </div>
    </Modal>
  )
}

interface UndoTrashBarProps {
  count: number
  secondsLeft: number
  loading: boolean
  onUndo: () => void
  onDismiss: () => void
}

/** 5-second undo affordance after moving recordings to the trash. */
export function UndoTrashBar({ count, secondsLeft, loading, onUndo, onDismiss }: UndoTrashBarProps) {
  const progress = Math.max(0, Math.min(100, (secondsLeft / 5) * 100))
  return (
    <div className="undo-trash-bar" role="status">
      <span className="undo-trash-bar__icon">
        <DeleteOutlined />
      </span>
      <div className="undo-trash-bar__body">
        <div className="undo-trash-bar__text">已将 {count} 条录音移入回收站</div>
        <div className="undo-trash-bar__sub">可在 {secondsLeft} 秒内撤销</div>
        <span className="undo-trash-bar__progress" style={{ width: `${progress}%` }} />
      </div>
      <Button size="small" type="primary" ghost loading={loading} onClick={onUndo}>
        撤销
      </Button>
      <button type="button" className="undo-trash-bar__close" aria-label="关闭" onClick={onDismiss}>
        <CloseOutlined />
      </button>
    </div>
  )
}

export interface BatchFailureListProps {
  items: { id: number; error: string | null }[]
}

/** Compact failure summary rendered inside toasts. */
export function BatchFailureList({ items }: BatchFailureListProps): ReactNode {
  return (
    <div className="batch-failure-list">
      {items.slice(0, 3).map((item) => (
        <div key={item.id}>#{item.id}：{item.error || '操作失败'}</div>
      ))}
      {items.length > 3 && <div>… 另有 {items.length - 3} 条失败</div>}
    </div>
  )
}

/** Selection summary row above the grid (select-all current page). */
export function SelectionRow({
  total,
  selectedCount,
  allSelected,
  partiallySelected,
  trashMode,
  onToggleAll,
  onClear,
}: {
  total: number
  selectedCount: number
  allSelected: boolean
  partiallySelected: boolean
  trashMode: boolean
  onToggleAll: (checked: boolean) => void
  onClear: () => void
}) {
  return (
    <div className="selection-row">
      <Checkbox
        checked={allSelected}
        indeterminate={partiallySelected}
        onChange={(event) => onToggleAll(event.target.checked)}
      >
        全选本页（{total} 条）
      </Checkbox>
      <span className="selection-row__hint">
        {selectedCount > 0 ? `已选 ${selectedCount} 条 · 仅当前页生效` : trashMode ? '回收站仅显示已删除录音' : '勾选录音可批量操作'}
      </span>
      {selectedCount > 0 && (
        <Button type="link" size="small" onClick={onClear}>
          取消选择
        </Button>
      )}
    </div>
  )
}
