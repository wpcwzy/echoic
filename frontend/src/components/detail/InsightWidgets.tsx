import { useEffect, useMemo, useState } from 'react'
import { Checkbox } from 'antd'
import { categoryMeta } from '../../theme'
import type { ActionItem, Insight } from '../../types'
import { formatDay } from '../../utils/format'

export function InsightCards({ insights }: { insights: Insight[] }) {
  return (
    <div className="insight-grid">
      {insights.map((item) => {
        const meta = categoryMeta[item.category] ?? categoryMeta.insight
        return (
          <article className="insight-card" key={item.id}>
            <header className="insight-card__head">
              <span className="insight-card__badge" style={{ color: meta.color, background: meta.bg }}>
                {meta.label}
              </span>
              <h4 className="insight-card__title">{item.title}</h4>
            </header>
            <p className="insight-card__content">{item.content}</p>
          </article>
        )
      })}
    </div>
  )
}

const CHECKLIST_PREFIX = 'echoic:action-items:'

export function useActionItemState(recordingId: number, items: ActionItem[] | null | undefined) {
  const storageKey = `${CHECKLIST_PREFIX}${recordingId}`
  const [state, setState] = useState<Record<string, boolean>>({})

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey)
      const parsed = raw ? (JSON.parse(raw) as Record<string, boolean>) : {}
      setState(parsed && typeof parsed === 'object' ? parsed : {})
    } catch {
      setState({})
    }
  }, [storageKey])

  const isChecked = (item: ActionItem): boolean => state[item.id] ?? item.status === 'done'

  const toggle = (id: string, checked: boolean) => {
    setState((prev) => {
      const next = { ...prev, [id]: checked }
      try {
        localStorage.setItem(storageKey, JSON.stringify(next))
      } catch {
        /* ignore */
      }
      return next
    })
  }

  return { isChecked, toggle }
}

export function ActionItemList({
  recordingId,
  items,
}: {
  recordingId: number
  items: ActionItem[] | null | undefined
}) {
  const list = useMemo(() => items ?? [], [items])
  const { isChecked, toggle } = useActionItemState(recordingId, list)

  if (!list.length) return <div className="muted-text">本次录音没有识别到待办事项</div>

  return (
    <ul className="todo-list">
      {list.map((item) => {
        const checked = isChecked(item)
        return (
          <li className={`todo-item ${checked ? 'is-done' : ''}`} key={item.id}>
            <Checkbox checked={checked} onChange={(event) => toggle(item.id, event.target.checked)}>
              <span className="todo-item__title">{item.title}</span>
            </Checkbox>
            <div className="todo-item__meta">
              {item.owner && <span className="todo-item__chip">负责人：{item.owner}</span>}
              {item.due && <span className="todo-item__chip">截止：{formatDaySafe(item.due)}</span>}
            </div>
            {item.detail && <p className="todo-item__detail">{item.detail}</p>}
          </li>
        )
      })}
    </ul>
  )
}

function formatDaySafe(value: string): string {
  if (!value) return ''
  const looksLikeIso = /^\d{4}-\d{2}-\d{2}/.test(value)
  return looksLikeIso ? formatDay(value) : value
}

export function KeywordCloud({ keywords }: { keywords: { name: string; weight: number }[] }) {
  const sorted = useMemo(() => [...keywords].sort((a, b) => b.weight - a.weight), [keywords])
  if (!sorted.length) return null
  const max = Math.max(...sorted.map((item) => item.weight), 1)
  return (
    <div className="keyword-cloud">
      {sorted.map((keyword, index) => {
        const ratio = Math.min(1, Math.max(0.1, keyword.weight / max))
        const fontSize = 12 + Math.round(ratio * 8)
        const opacity = 0.55 + ratio * 0.45
        return (
          <span
            key={`${keyword.name}-${index}`}
            className={`keyword-tag ${index < 3 ? 'keyword-tag--hot' : ''}`}
            style={{ fontSize, opacity }}
          >
            {keyword.name}
          </span>
        )
      })}
    </div>
  )
}
