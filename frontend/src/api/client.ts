import type { SSEEvent } from '../types'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, message: string, code = 'ERROR') {
    super(message)
    this.status = status
    this.code = code
  }
}

function buildUrl(path: string): string {
  return path.startsWith('/') ? path : `/${path}`
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  rawBody?: BodyInit
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, rawBody, headers, ...rest } = options
  const finalHeaders = new Headers(headers)
  let payload: BodyInit | undefined = rawBody
  if (body !== undefined) {
    finalHeaders.set('Content-Type', 'application/json')
    payload = JSON.stringify(body)
  }
  const response = await fetch(buildUrl(path), {
    credentials: 'include',
    ...rest,
    headers: finalHeaders,
    body: payload,
  })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    let code = 'ERROR'
    try {
      const data = await response.json()
      message = data?.detail || message
      code = data?.code || code
    } catch {
      /* ignore non-json error */
    }
    if (response.status === 401 && !window.location.pathname.startsWith('/login')) {
      window.location.href = '/login'
    }
    throw new ApiError(response.status, message, code)
  }
  if (response.status === 204) {
    return undefined as T
  }
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    return (await response.json()) as T
  }
  return (await response.text()) as unknown as T
}

export const api = {
  get: <T>(path: string) => request<T>(path, { method: 'GET' }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData, onProgress?: (percent: number) => void) =>
    new Promise<T>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', buildUrl(path))
      xhr.withCredentials = true
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      }
      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText || '{}')
          if (xhr.status >= 200 && xhr.status < 300) resolve(data as T)
          else reject(new ApiError(xhr.status, data?.detail || '上传失败', data?.code))
        } catch {
          reject(new ApiError(xhr.status, '上传失败：响应解析错误'))
        }
      }
      xhr.onerror = () => reject(new ApiError(0, '网络错误，上传失败'))
      xhr.send(form)
    }),
}

/** Subscribe to the chat SSE endpoint. Returns an abort function. */
export function streamChat(
  recordingId: number,
  message: string,
  handlers: {
    onEvent: (event: SSEEvent) => void
    onError?: (error: Error) => void
    onClose?: () => void
  },
): () => void {
  const controller = new AbortController()
  const run = async () => {
    try {
      const response = await fetch(`/api/recordings/${recordingId}/chat`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        let detail = '问答请求失败'
        try {
          const data = await response.json()
          detail = data?.detail || detail
        } catch {
          /* ignore */
        }
        throw new ApiError(response.status, detail)
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() || ''
        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data:')) continue
            const payload = line.slice(5).trim()
            if (!payload) continue
            try {
              handlers.onEvent(JSON.parse(payload) as SSEEvent)
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      }
      handlers.onClose?.()
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        handlers.onError?.(error as Error)
      }
    }
  }
  void run()
  return () => controller.abort()
}
