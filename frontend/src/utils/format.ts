import dayjs from 'dayjs'
import { speakerPalette } from '../theme'

/** Zero-pad a number to at least 2 digits. */
export function pad2(value: number): string {
  return Math.floor(Math.abs(value)).toString().padStart(2, '0')
}

/**
 * Format a millisecond duration as `mm:ss`, or `h:mm:ss` when >= 1 hour.
 * `alwaysHours` forces the hour segment.
 */
export function formatMs(ms: number | null | undefined, alwaysHours = false): string {
  const total = Math.max(0, Math.floor((ms ?? 0) / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0 || alwaysHours) return `${hours}:${pad2(minutes)}:${pad2(seconds)}`
  return `${pad2(minutes)}:${pad2(seconds)}`
}

/** `mm:ss` style clock used in players (same as formatMs, kept explicit). */
export function formatClock(ms: number | null | undefined): string {
  return formatMs(ms)
}

/** Human readable duration, e.g. `1 小时 23 分` / `12 分 08 秒` / `42 秒`. */
export function formatDurationHuman(ms: number | null | undefined, short = false): string {
  const total = Math.max(0, Math.round((ms ?? 0) / 1000))
  if (total === 0) return short ? '0 分钟' : '00:00'
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (short) {
    if (hours > 0) {
      const rest = Math.round((minutes / 60) * 10) / 10
      return `${hours + rest} 小时`
    }
    return `${Math.max(1, Math.round(total / 60))} 分钟`
  }
  if (hours > 0) return `${hours} 小时 ${pad2(minutes)} 分`
  if (minutes > 0) return `${minutes} 分 ${pad2(seconds)} 秒`
  return `${seconds} 秒`
}

/** Hours with one decimal, e.g. `2.3`. */
export function formatHours(ms: number | null | undefined): number {
  return Math.round(((ms ?? 0) / 3_600_000) * 10) / 10
}

/** `YYYY-MM-DD HH:mm`. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return dayjs(iso).format('YYYY-MM-DD HH:mm')
}

/** `YYYY-MM-DD`. */
export function formatDay(iso: string | null | undefined): string {
  if (!iso) return '—'
  return dayjs(iso).format('YYYY-MM-DD')
}

/** `MM-DD HH:mm`. */
export function formatDateShort(iso: string | null | undefined): string {
  if (!iso) return '—'
  return dayjs(iso).format('MM-DD HH:mm')
}

/** Friendly semi-relative date, e.g. `刚刚`, `3 分钟前`, `今天 14:06`, `昨天 09:12`, `10-21 18:30`. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = dayjs(iso)
  const now = dayjs()
  const diffSec = now.diff(date, 'second')
  if (diffSec < 0) return date.format('MM-DD HH:mm')
  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (date.isSame(now, 'day')) return `今天 ${date.format('HH:mm')}`
  if (date.isSame(now.subtract(1, 'day'), 'day')) return `昨天 ${date.format('HH:mm')}`
  if (date.isSame(now, 'year')) return date.format('MM-DD HH:mm')
  return date.format('YYYY-MM-DD HH:mm')
}

/** `1.2 GB` / `860 KB` / `—`. */
export function formatBytes(bytes: number | null | undefined): string {
  const value = bytes ?? 0
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)))
  const num = value / Math.pow(1024, index)
  const digits = num >= 100 || index === 0 ? 0 : num >= 10 ? 1 : 2
  return `${num.toFixed(digits)} ${units[index]}`
}

export function greeting(): string {
  const hour = new Date().getHours()
  if (hour < 6) return '夜深了'
  if (hour < 9) return '早上好'
  if (hour < 12) return '上午好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
}

/** First 1–2 visible characters, used for fallback avatars. */
export function initials(name: string | null | undefined, max = 2): string {
  const value = (name || '').trim()
  if (!value) return '?'
  if (/^[\x00-\x7F]+$/.test(value)) {
    const words = value.split(/\s+/).filter(Boolean)
    if (words.length >= 2) {
      return (words[0][0] + words[words.length - 1][0]).toUpperCase()
    }
    return value.slice(0, max).toUpperCase()
  }
  // Chinese & other CJK names: use the tail characters ("吴克强" -> "克强")
  return value.length > max ? value.slice(value.length - max) : value
}

/** Deterministic color from a name (speaker fallback color). */
export function colorFromString(input: string | null | undefined): string {
  const value = input || 'guest'
  let hash = 0
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) % 100000
  }
  return speakerPalette[hash % speakerPalette.length]
}

/** Turn `#RRGGBB` into `rgba(r, g, b, alpha)`. */
export function hexToRgba(hex: string, alpha: number): string {
  const normalized = hex.replace('#', '')
  const full = normalized.length === 3 ? normalized.split('').map((c) => c + c).join('') : normalized
  const num = Number.parseInt(full, 16)
  if (Number.isNaN(num)) return `rgba(22, 119, 255, ${alpha})`
  const r = (num >> 16) & 255
  const g = (num >> 8) & 255
  const b = num & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** Read a `?t=1234` style millisecond cursor from URLSearchParams. */
export function parseMsParam(value: string | null): number | null {
  if (!value) return null
  const num = Number(value)
  return Number.isFinite(num) && num >= 0 ? num : null
}

export function pluralizeCn(count: number, unit: string): string {
  return `${count} ${unit}`
}

/** Split text into fragments, flagging case-insensitive matches of `query` for <mark>. */
export function splitHighlight(text: string, query: string): { text: string; hit: boolean }[] {
  const needle = query.trim()
  if (!needle) return [{ text, hit: false }]
  const parts: { text: string; hit: boolean }[] = []
  const lower = text.toLowerCase()
  const target = needle.toLowerCase()
  let index = 0
  while (index < text.length) {
    const found = lower.indexOf(target, index)
    if (found < 0) {
      parts.push({ text: text.slice(index), hit: false })
      break
    }
    if (found > index) parts.push({ text: text.slice(index, found), hit: false })
    parts.push({ text: text.slice(found, found + target.length), hit: true })
    index = found + target.length
  }
  return parts.length ? parts : [{ text, hit: false }]
}

/** Friendly error message for API/provider failures. */
export function humanizeError(error: unknown, fallback = '操作失败，请稍后重试'): string {
  if (error && typeof error === 'object') {
    const maybe = error as { message?: string; status?: number; code?: string }
    if (maybe.message && maybe.message.trim()) {
      if (maybe.code === 'PROVIDER_ERROR') return `AI 服务调用失败：${maybe.message}`
      return maybe.message
    }
  }
  if (typeof error === 'string' && error.trim()) return error
  return fallback
}

export function dateStamp(date = new Date()): string {
  return dayjs(date).format('YYYYMMDD-HHmm')
}
