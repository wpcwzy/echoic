import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Input, Tooltip } from 'antd'
import {
  ApartmentOutlined,
  ArrowRightOutlined,
  BulbOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  DeleteOutlined,
  RobotOutlined,
  SendOutlined,
  StopFilled,
  VerticalRightOutlined,
} from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { api, streamChat } from '../../api/client'
import { qk, useChatHistory } from '../../api/queries'
import type { ChatMessage, Citation, Insights, SSEEvent } from '../../types'
import { formatMs } from '../../utils/format'
import { confirmAction, toast, toastError } from '../../utils/toast'
import { MarkdownView } from '../common/MarkdownView'

interface UiMessage extends ChatMessage {
  streaming?: boolean
  stopped?: boolean
  error?: string
}

interface AIPanelProps {
  recordingId: number
  enabled: boolean
  insights: Insights | undefined
  onOpenMindMap: () => void
  onOpenInsights: () => void
  onSeek: (ms: number) => void
  onCollapse?: () => void
  variant?: 'inline' | 'drawer'
}

const DEFAULT_QUESTIONS = ['这场会议的主要结论是什么？', '帮我提炼一下重点内容', '会议定下了哪些待办事项？']
const QUESTION_EMOJIS = ['✏️', '📄', '💡', '📚', '🎯', '🔍']

function CitationChips({ citations, onSeek }: { citations: Citation[]; onSeek: (ms: number) => void }) {
  if (!citations?.length) return null
  return (
    <div className="chat-citations">
      {citations.map((citation, index) => (
        <Tooltip
          key={`${citation.segment_id ?? 'c'}-${citation.start_ms}-${index}`}
          placement="top"
          title={
            <span className="citation-tooltip">
              {citation.speaker ? `${citation.speaker}：` : ''}
              {citation.text}
            </span>
          }
        >
          <button type="button" className="chat-citation" onClick={() => onSeek(citation.start_ms)}>
            <ClockCircleOutlined />
            <span>{formatMs(citation.start_ms)}</span>
            {citation.speaker && <span className="chat-citation__speaker">{citation.speaker}</span>}
          </button>
        </Tooltip>
      ))}
    </div>
  )
}

function MessageBubble({ message, onSeek }: { message: UiMessage; onSeek: (ms: number) => void }) {
  const isUser = message.role === 'user'
  if (isUser) {
    return (
      <div className="chat-row chat-row--user">
        <div className="chat-bubble chat-bubble--user">{message.content}</div>
      </div>
    )
  }
  return (
    <div className="chat-row chat-row--assistant">
      <div className="chat-avatar">
        <RobotOutlined />
      </div>
      <div className="chat-bubble chat-bubble--assistant">
        <MarkdownView
          content={message.content}
          onTimestampClick={onSeek}
          className="chat-markdown"
          emptyText={message.streaming ? '' : '（无内容）'}
        />
        {message.streaming && <span className="chat-cursor" aria-hidden="true" />}
        {message.error && <div className="chat-error">{message.error}</div>}
        {message.stopped && <div className="chat-stopped">已停止生成</div>}
        <CitationChips citations={message.citations ?? []} onSeek={onSeek} />
      </div>
    </div>
  )
}

/**
 * Right-hand AI assistant panel: quick actions, suggested questions, streaming
 * chat with markdown + clickable timestamp citations, and composer.
 */
export function AIPanel({
  recordingId,
  enabled,
  insights,
  onOpenMindMap,
  onOpenInsights,
  onSeek,
  onCollapse,
  variant = 'inline',
}: AIPanelProps) {
  const history = useChatHistory(recordingId, enabled)
  const queryClient = useQueryClient()
  const [messages, setMessages] = useState<UiMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)
  const initializedRef = useRef(false)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    abortRef.current?.()
    abortRef.current = null
    setStreaming(false)
    setMessages([])
    setInput('')
    initializedRef.current = false
  }, [recordingId])

  useEffect(() => {
    if (!initializedRef.current && history.data) {
      initializedRef.current = true
      setMessages(history.data.messages.map((message) => ({ ...message })))
    }
  }, [history.data])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, streaming])

  useEffect(() => {
    return () => {
      abortRef.current?.()
    }
  }, [])

  const suggested = useMemo(() => {
    const fromServer = (insights?.suggested_questions ?? []).filter((question) => question.trim().length > 0)
    return (fromServer.length ? fromServer : DEFAULT_QUESTIONS).slice(0, 6)
  }, [insights])

  const hasViewpoints = Boolean(insights?.viewpoint_tree?.root)
  const hasInsights = Boolean(insights?.insights?.length || insights?.action_items?.length)

  const send = useCallback(
    (raw?: string) => {
      const content = (raw ?? input).trim()
      if (!content || streaming || !enabled) return
      setInput('')
      const userMessage: UiMessage = {
        id: -Date.now(),
        role: 'user',
        content,
        citations: [],
        created_at: new Date().toISOString(),
      }
      const assistantKey = -(Date.now() + 7)
      setMessages((prev) => [...prev, userMessage, { id: assistantKey, role: 'assistant', content: '', citations: [], streaming: true }])
      setStreaming(true)

      abortRef.current = streamChat(recordingId, content, {
        onEvent: (event: SSEEvent) => {
          if (event.type === 'delta') {
            setMessages((prev) =>
              prev.map((message) =>
                message.id === assistantKey ? { ...message, content: message.content + event.text } : message,
              ),
            )
          } else if (event.type === 'citations') {
            setMessages((prev) =>
              prev.map((message) =>
                message.id === assistantKey ? { ...message, citations: event.citations ?? [] } : message,
              ),
            )
          } else if (event.type === 'done') {
            setMessages((prev) =>
              prev.map((message) => (message.id === assistantKey ? { ...message, streaming: false } : message)),
            )
            setStreaming(false)
            void queryClient.invalidateQueries({ queryKey: qk.recording(recordingId) })
          } else if (event.type === 'error') {
            const message = event.message || 'AI 生成失败，请重试'
            setMessages((prev) =>
              prev.map((item) =>
                item.id === assistantKey ? { ...item, streaming: false, error: message } : item,
              ),
            )
            setStreaming(false)
            toast.error(message)
          }
        },
        onError: (error) => {
          setStreaming(false)
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantKey
                ? { ...message, streaming: false, error: error.message || '网络异常，请稍后重试' }
                : message,
            ),
          )
        },
        onClose: () => setStreaming(false),
      })
    },
    [enabled, input, queryClient, recordingId, streaming],
  )

  const stopStreaming = () => {
    abortRef.current?.()
    abortRef.current = null
    setStreaming(false)
    setMessages((prev) => prev.map((message) => (message.streaming ? { ...message, streaming: false, stopped: true } : message)))
  }

  const clearChat = () => {
    if (!messages.length) {
      toast.info('当前没有问答记录')
      return
    }
    confirmAction({
      title: '清空问答记录？',
      content: '将删除本场录音的所有 AI 问答历史，此操作无法恢复。',
      danger: true,
      okText: '清空',
      onOk: async () => {
        try {
          await api.delete(`/api/recordings/${recordingId}/chat`)
          setMessages([])
          queryClient.setQueryData(qk.chat(recordingId), { messages: [] })
          toast.success('问答记录已清空')
        } catch (error) {
          toastError(error, '清空问答记录失败')
        }
      },
    })
  }

  return (
    <div className={`ai-panel ai-panel--${variant}`}>
      <header className="ai-panel__header">
        <div className="ai-panel__title">
          <span className="ai-panel__title-icon">
            <RobotOutlined />
          </span>
          AI 问答
        </div>
        <div className="ai-panel__actions">
          <Tooltip title="清空会话">
            <button type="button" className="ai-icon-btn" aria-label="清空会话" onClick={clearChat}>
              <DeleteOutlined />
            </button>
          </Tooltip>
          {onCollapse && (
            <Tooltip title={variant === 'drawer' ? '关闭面板' : '收起面板'}>
              <button type="button" className="ai-icon-btn" aria-label="收起 AI 面板" onClick={onCollapse}>
                {variant === 'drawer' ? <CloseOutlined /> : <VerticalRightOutlined />}
              </button>
            </Tooltip>
          )}
        </div>
      </header>

      <div className="ai-panel__quick">
        <Tooltip title={hasViewpoints ? '查看思维导图' : 'AI 分析完成后可用'}>
          <button
            type="button"
            className="ai-quick ai-quick--mind"
            onClick={onOpenMindMap}
            disabled={!hasViewpoints}
          >
            <ApartmentOutlined className="ai-quick__icon" />
            <span>思维导图</span>
          </button>
        </Tooltip>
        <Tooltip title={hasInsights ? '查看关键洞察' : 'AI 分析完成后可用'}>
          <button
            type="button"
            className="ai-quick ai-quick--insight"
            onClick={onOpenInsights}
            disabled={!hasInsights}
          >
            <BulbOutlined className="ai-quick__icon" />
            <span>关键洞察</span>
          </button>
        </Tooltip>
      </div>

      <div className="ai-panel__scroll">
        {!enabled ? (
          <div className="ai-disabled-note">
            <div className="ai-disabled-note__icon">
              <RobotOutlined />
            </div>
            <div className="ai-disabled-note__title">AI 问答尚未就绪</div>
            <div className="ai-disabled-note__desc">录音处理完成后，即可基于转写内容向 AI 提问。</div>
          </div>
        ) : messages.length === 0 ? (
          <>
            <div className="ai-suggested">
              <div className="ai-suggested__label">猜你想问</div>
              <div className="ai-suggested__list">
                {suggested.map((question, index) => (
                  <button type="button" className="ai-question" key={question} onClick={() => send(question)}>
                    <span className="ai-question__emoji">{QUESTION_EMOJIS[index % QUESTION_EMOJIS.length]}</span>
                    <span className="ai-question__text">{question}</span>
                    <ArrowRightOutlined className="ai-question__arrow" />
                  </button>
                ))}
              </div>
            </div>

            <div className="ai-welcome">
              <div className="ai-welcome__icon">
                <RobotOutlined />
              </div>
              <div className="ai-welcome__title">你好，我是 Echoic AI</div>
              <div className="ai-welcome__desc">
                关于这场录音的任何问题都可以问我，回答会标注原文时间点，点击即可跳转到播放位置。
              </div>
            </div>
          </>
        ) : (
          <div className="chat-messages">
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} onSeek={onSeek} />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="ai-composer">
        <div className="ai-composer__box">
          <Input.TextArea
            value={input}
            variant="borderless"
            placeholder={enabled ? '有问题尽管问我…' : '录音处理完成后即可提问'}
            autoSize={{ minRows: 1, maxRows: 5 }}
            disabled={!enabled}
            onChange={(event) => setInput(event.target.value)}
            onPressEnter={(event) => {
              if (event.shiftKey) return
              if ((event.nativeEvent as KeyboardEvent).isComposing) return
              event.preventDefault()
              send()
            }}
          />
          <div className="ai-composer__bar">
            <span className="ai-composer__hint">Enter 发送 · Shift + Enter 换行</span>
            {streaming ? (
              <Tooltip title="停止生成">
                <button type="button" className="ai-send ai-send--stop" aria-label="停止生成" onClick={stopStreaming}>
                  <StopFilled />
                </button>
              </Tooltip>
            ) : (
              <Tooltip title={enabled ? '发送' : '处理完成后可提问'}>
                <button
                  type="button"
                  className="ai-send"
                  aria-label="发送"
                  disabled={!enabled || !input.trim()}
                  onClick={() => send()}
                >
                  <SendOutlined />
                </button>
              </Tooltip>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
