import { cloneElement, Fragment, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface MarkdownViewProps {
  content: string
  className?: string
  /** When provided, inline `[12:05]` timestamps become clickable seek buttons. */
  onTimestampClick?: (ms: number) => void
  emptyText?: string
}

function parseTimestamp(label: string): number | null {
  const parts = label.split(':').map((part) => Number(part))
  if (parts.some((value) => !Number.isFinite(value) || value < 0)) return null
  if (parts.length === 2) return (parts[0] * 60 + parts[1]) * 1000
  if (parts.length === 3) return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000
  return null
}

function renderTimestamped(node: ReactNode, onTimestampClick?: (ms: number) => void): ReactNode {
  if (node === null || node === undefined || typeof node === 'boolean') return node
  if (typeof node === 'number') return node
  if (typeof node === 'string') {
    if (!onTimestampClick || !node.includes('[')) return node
    const pattern = /\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g
    const parts: ReactNode[] = []
    let lastIndex = 0
    let match = pattern.exec(node)
    let key = 0
    while (match) {
      if (match.index > lastIndex) parts.push(node.slice(lastIndex, match.index))
      const ms = parseTimestamp(match[1])
      if (ms !== null) {
        parts.push(
          <button
            key={`ts-${key++}`}
            type="button"
            className="md-timestamp"
            onClick={() => onTimestampClick(ms)}
            title="跳转到该时间点"
          >
            {match[1]}
          </button>,
        )
      } else {
        parts.push(match[0])
      }
      lastIndex = match.index + match[0].length
      match = pattern.exec(node)
    }
    if (lastIndex < node.length) parts.push(node.slice(lastIndex))
    return parts.length ? parts.map((part, index) => <Fragment key={index}>{part}</Fragment>) : node
  }
  if (Array.isArray(node)) {
    return node.map((child, index) => <Fragment key={index}>{renderTimestamped(child, onTimestampClick)}</Fragment>)
  }
  if (isValidElement<{ children?: ReactNode }>(node) && node.props?.children !== undefined) {
    return cloneElement(node, { children: renderTimestamped(node.props.children, onTimestampClick) })
  }
  return node
}

/**
 * Markdown renderer tuned for AI summaries & chat answers (GFM tables, lists,
 * code blocks). Bare `[mm:ss]` timestamps become seek buttons when a handler is
 * supplied; external images are intentionally not rendered.
 */
export function MarkdownView({ content, className, onTimestampClick, emptyText = '暂无内容' }: MarkdownViewProps) {
  const trimmed = (content || '').trim()
  if (!trimmed) return <div className={`markdown-view markdown-view--empty ${className ?? ''}`}>{emptyText}</div>

  const wrap = (children: ReactNode) =>
    onTimestampClick ? renderTimestamped(children, onTimestampClick) : children

  return (
    <div className={`markdown-view ${className ?? ''}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{wrap(children)}</p>,
          li: ({ children }) => <li>{wrap(children)}</li>,
          h1: ({ children }) => <h3 className="md-h">{wrap(children)}</h3>,
          h2: ({ children }) => <h4 className="md-h">{wrap(children)}</h4>,
          h3: ({ children }) => <h5 className="md-h">{wrap(children)}</h5>,
          h4: ({ children }) => <h6 className="md-h">{wrap(children)}</h6>,
          h5: ({ children }) => <h6 className="md-h">{wrap(children)}</h6>,
          h6: ({ children }) => <h6 className="md-h">{wrap(children)}</h6>,
          strong: ({ children }) => <strong className="md-strong">{wrap(children)}</strong>,
          em: ({ children }) => <em>{wrap(children)}</em>,
          a: ({ children, href }) => (
            <a className="md-link" href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          code: ({ className: codeClassName, children }) => {
            const isBlock = Boolean(codeClassName?.includes('language-'))
            if (isBlock) return <code className={codeClassName}>{children}</code>
            return <code className="md-inline-code">{children}</code>
          },
          pre: ({ children }) => <pre className="md-pre">{children}</pre>,
          blockquote: ({ children }) => <blockquote className="md-blockquote">{children}</blockquote>,
          table: ({ children }) => (
            <div className="md-table-wrap">
              <table>{children}</table>
            </div>
          ),
          hr: () => <hr className="md-hr" />,
          img: () => null,
        }}
      >
        {trimmed}
      </ReactMarkdown>
    </div>
  )
}
