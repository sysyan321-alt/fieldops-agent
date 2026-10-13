/**
 * 后端 API 基地址解析（本机 + 局域网通用）。
 *
 * - 未配置 VITE_API_BASE_URL：返回空串，走同源（Vite proxy / Nginx 反代）
 * - 配置的是 localhost / 127.0.0.1 / ::1：把主机名换成当前页面的主机名，
 *   于是本机用 http://localhost:3000 打开、手机用 http://192.168.x.x:3000 打开，
 *   都会各自直连到同一台后端的 8000 端口（SSE 也走直连，避免代理缓冲）
 * - 其它值：按配置原样使用（例如生产环境 https://api.example.com）
 */
const configured = import.meta.env.VITE_API_BASE_URL as string | undefined

const LOCAL_HOSTS = ['localhost', '127.0.0.1', '::1', '[::1]']

function resolveApiBase(): string {
  if (!configured) return ''
  try {
    const url = new URL(configured)
    if (LOCAL_HOSTS.includes(url.hostname) && typeof window !== 'undefined') {
      url.hostname = window.location.hostname
    }
    return url.origin
  } catch {
    return configured
  }
}

export const API_BASE = resolveApiBase()
