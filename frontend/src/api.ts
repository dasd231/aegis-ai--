import type { StreamEvent } from './types'

const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'
const PORTAL = window.location.pathname.startsWith('/admin') ? 'admin' : 'user'
const TOKEN_KEY = `aegis_${PORTAL}_access_token`

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

function localizedValidationMessage(message: string): string {
  const normalized = message.replace(/^Value error,\s*/i, '').trim()
  if (/field required/i.test(normalized)) return '存在未填写的必填项'
  if (/valid email address/i.test(normalized)) return '邮箱格式不正确'
  if (/valid string/i.test(normalized)) return '输入内容必须是文本'
  if (/valid integer|valid number/i.test(normalized)) return '输入内容必须是数字'
  if (/at least \d+ characters?/i.test(normalized)) {
    const length = normalized.match(/at least (\d+)/i)?.[1]
    return `输入内容不能少于 ${length ?? '规定'} 个字符`
  }
  if (/at most \d+ characters?/i.test(normalized)) {
    const length = normalized.match(/at most (\d+)/i)?.[1]
    return `输入内容不能超过 ${length ?? '规定'} 个字符`
  }
  if (/input should be/i.test(normalized)) return '输入内容不符合要求'
  return normalized
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== 'object') return fallback
  const detail = (payload as { detail?: unknown }).detail
  if (typeof detail === 'string') return localizedValidationMessage(detail)
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? localizedValidationMessage(String(item.msg)) : ''))
      .filter(Boolean)
      .join('；') || fallback
  }
  return fallback
}

async function fetchWithChineseError(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('无法连接服务，请检查网络后重试')
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const token = tokenStore.get()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetchWithChineseError(`${API_BASE}${path}`, { ...options, headers })
  if (response.status === 401) {
    tokenStore.clear()
    window.dispatchEvent(new CustomEvent('aegis:auth-expired'))
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new ApiError(errorMessage(payload, `请求失败（${response.status}）`), response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function parseFrame(frame: string): StreamEvent | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (!dataLines.length) return null
  try {
    return { type: event, data: JSON.parse(dataLines.join('\n')) } as StreamEvent
  } catch {
    throw new Error('服务端返回了无法解析的流事件')
  }
}

export async function streamChat(
  body: { session_id: string; question: string; knowledge_base_id?: string },
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const token = tokenStore.get()
  const response = await fetchWithChineseError(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
    signal,
  })
  if (response.status === 401) {
    tokenStore.clear()
    window.dispatchEvent(new CustomEvent('aegis:auth-expired'))
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new ApiError(errorMessage(payload, `请求失败（${response.status}）`), response.status)
  }
  if (!response.body) throw new Error('当前浏览器不支持流式响应')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminalEventSeen = false
  const deliver = (event: StreamEvent | null) => {
    if (!event) return
    if (event.type === 'done' || event.type === 'error') terminalEventSeen = true
    onEvent(event)
  }
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      deliver(parseFrame(frame))
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) {
    deliver(parseFrame(buffer))
  }
  if (!terminalEventSeen) throw new Error('流式连接意外中断，回答可能不完整，请重试')
}
