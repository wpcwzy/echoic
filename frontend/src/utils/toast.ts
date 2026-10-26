import { message as staticMessage, Modal as staticModal } from 'antd'
import type { ReactNode } from 'react'
import { ApiError } from '../api/client'
import { humanizeError } from './format'

interface MessageApiLike {
  success: (content: ReactNode) => void
  error: (content: ReactNode) => void
  info: (content: ReactNode) => void
  warning: (content: ReactNode) => void
  loading: (content: ReactNode) => void
}

interface ModalApiLike {
  confirm: (options: {
    title: ReactNode
    content?: ReactNode
    okText?: string
    cancelText?: string
    okButtonProps?: Record<string, unknown>
    onOk?: () => void | Promise<void>
  }) => void
}

interface FeedbackBridge {
  message: MessageApiLike
  modal: ModalApiLike
}

let bridge: FeedbackBridge | null = null

/**
 * Called once from the app root with AntD's context-aware `App.useApp()` handles.
 * Keeps static helper calls (outside React) theme-aware.
 */
export function bindFeedback(next: unknown): void {
  bridge = next as FeedbackBridge
}

const fallbackMessage: MessageApiLike = {
  success: (content) => void staticMessage.success(content as string),
  error: (content) => void staticMessage.error(content as string),
  info: (content) => void staticMessage.info(content as string),
  warning: (content) => void staticMessage.warning(content as string),
  loading: (content) => void staticMessage.loading(content as string),
}

const fallbackModal: ModalApiLike = {
  confirm: (options) => {
    staticModal.confirm({
      title: options.title,
      content: options.content,
      okText: options.okText,
      cancelText: options.cancelText,
      onOk: options.onOk,
    })
  },
}

/** Global toast helpers usable from anywhere (components, hooks, event handlers). */
export const toast = {
  success(content: ReactNode): void {
    ;(bridge?.message ?? fallbackMessage).success(content)
  },
  error(content: ReactNode): void {
    ;(bridge?.message ?? fallbackMessage).error(content)
  },
  info(content: ReactNode): void {
    ;(bridge?.message ?? fallbackMessage).info(content)
  },
  warning(content: ReactNode): void {
    ;(bridge?.message ?? fallbackMessage).warning(content)
  },
}

/** Toast a friendly message for an unknown thrown error. */
export function toastError(error: unknown, fallback = '操作失败，请稍后重试'): void {
  if (error instanceof ApiError && error.status === 401) {
    return
  }
  toast.error(humanizeError(error, fallback))
}

/** Confirm dialog that integrates with the AntD context theme. */
export function confirmAction(options: {
  title: ReactNode
  content?: ReactNode
  okText?: string
  cancelText?: string
  danger?: boolean
  onOk?: () => void | Promise<void>
}): void {
  ;(bridge?.modal ?? fallbackModal).confirm({
    title: options.title,
    content: options.content,
    okText: options.okText ?? '确定',
    cancelText: options.cancelText ?? '取消',
    okButtonProps: options.danger ? { danger: true } : undefined,
    onOk: options.onOk,
  })
}
