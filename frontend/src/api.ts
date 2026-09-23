import type { Book, BookDetail, Page, Settings } from './types'

const API_PREFIX = '/api'

function detailMessage(value: unknown, fallback: string): string {
  if (typeof value === 'object' && value && 'detail' in value) {
    const detail = (value as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit, responseType: 'json' | 'text' = 'json'): Promise<T> {
  const requestPath = `${API_PREFIX}${path}`
  let response: Response
  try {
    response = await fetch(requestPath, init)
  } catch {
    throw new Error(`无法连接本地服务（请求 ${requestPath}），请确认后端已启动。`)
  }

  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = null
    }
    const detail = detailMessage(body, '')
    const suffix = detail ? `：${detail}` : ''
    throw new Error(`本地接口 ${requestPath} 请求失败（HTTP ${response.status}）${suffix}`)
  }
  if (response.status === 204) return undefined as T
  if (responseType === 'text') return response.text() as Promise<T>
  return response.json() as Promise<T>
}

export const api = {
  listBooks: () => request<Book[]>('/books'),
  getBook: (id: string) => request<BookDetail>(`/books/${encodeURIComponent(id)}`),
  deleteBook: (id: string) => request<void>(`/books/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  uploadBook: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Book>('/books', { method: 'POST', body: form })
  },
  processBook: (id: string) =>
    request<{ started: boolean }>(`/books/${encodeURIComponent(id)}/process`, {
      method: 'POST',
    }),
  pauseBook: (id: string) =>
    request<{ requested: boolean }>(`/books/${encodeURIComponent(id)}/pause`, {
      method: 'POST',
    }),
  savePage: (bookId: string, pageNumber: number, text: string) =>
    request<Page>(
      `/books/${encodeURIComponent(bookId)}/pages/${encodeURIComponent(pageNumber)}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      },
    ),
  getSettings: () => request<Settings>('/settings'),
  saveSettings: (settings: Settings, clearApiKey: boolean) =>
    request<Settings>('/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: settings.base_url,
        responses_path: settings.responses_path,
        extraction_model: settings.extraction_model,
        reasoning_effort: settings.reasoning_effort,
        classification_model: settings.classification_model,
        api_key: settings.api_key || '',
        structured_output: false,
        max_output_tokens: settings.max_output_tokens,
        timeout_seconds: settings.timeout_seconds,
        clear_api_key: clearApiKey,
      }),
    }),
  testSettings: () => request<{ ok: boolean; message: string }>('/settings/test', { method: 'POST' }),
  exportBook: (id: string) => request<BookDetail>(`/books/${encodeURIComponent(id)}/export`),
  exportMarkdown: (id: string) => request<string>(`/books/${encodeURIComponent(id)}/export.md`, undefined, 'text'),
}
