/**
 * 通用 SSE 客户端：消费后端 EventSourceResponse 的命名事件帧。
 *
 * 帧格式（sse-starlette）：
 *   event: answer
 *   data: {"content": "..."}
 *
 * 直接 fetch 后端（不经 Vite proxy），避免代理缓冲 SSE 帧。
 */

import { API_BASE } from './base'

export type SSEEventHandler = (event: string, data: any) => void

export async function streamSSE(
  path: string,
  body: Record<string, unknown>,
  onEvent: SSEEventHandler,
  token?: string | null,
): Promise<void> {
  const apiBase = API_BASE

  const resp = await fetch(`${apiBase}/api/v1${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  })

  if (resp.status === 401) {
    throw new UnauthorizedError()
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buf += decoder.decode(value, { stream: true })
    // 兼容 sse-starlette 的 \r\n 行尾，统一为 \n
    buf = buf.replace(/\r\n/g, '\n')
    // SSE 每条消息以 "\n\n" 结尾
    const parts = buf.split('\n\n')
    buf = parts.pop() ?? ''

    for (const part of parts) {
      let event = 'message'
      let data = ''
      for (const line of part.split('\n')) {
        if (line.startsWith('event:')) {
          event = line.slice(6).trim()
        } else if (line.startsWith('data:')) {
          data += line.slice(5).trim()
        }
      }
      if (!data) continue
      try {
        onEvent(event, data ? JSON.parse(data) : {})
      } catch {
        // 忽略非 JSON 数据
      }
    }
  }
}

export class UnauthorizedError extends Error {
  constructor() {
    super('未登录或登录已过期')
    this.name = 'UnauthorizedError'
  }
}
