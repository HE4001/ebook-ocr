import type { Arrangement, Book, BookDetail, Page, PageDraft, PaperSize, Settings } from './types'

const API_PREFIX = '/api'

function detailMessage(value: unknown, fallback: string): string {
  if (typeof value === 'object' && value && 'detail' in value) {
    const detail = (value as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  return response.json() as Promise<T>
}

export const api = {
  listBooks: () => request<Book[]>('/books'),
  createProject: (title: string) => request<Book>('/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  }),
  uploadFiles: (id: string, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<Arrangement>(`/books/${encodeURIComponent(id)}/files`, { method: 'POST', body: form })
  },
  confirmUpload: (id: string) => request<Arrangement>(`/books/${encodeURIComponent(id)}/confirm-upload`, { method: 'POST' }),
  getArrangement: (id: string) => request<Arrangement>(`/books/${encodeURIComponent(id)}/arrangement`),
  saveArrangement: (id: string, order: { file_order: string[]; file_parents: Record<string, string | null>; page_order: number[] }) =>
    request<BookDetail>(`/books/${encodeURIComponent(id)}/arrangement`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(order),
    }),
  pagePreviewUrl: (id: string, number: number) => `${API_PREFIX}/books/${encodeURIComponent(id)}/pages/${number}/preview`,
  getBook: (id: string) => request<BookDetail>(`/books/${encodeURIComponent(id)}`),
  saveBookLayout: (id: string, paperSize: PaperSize) => request<Book>(`/books/${encodeURIComponent(id)}/layout`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paper_size: paperSize }),
  }),
  deleteBook: (id: string) => request<void>(`/books/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  uploadBook: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Book>('/books', { method: 'POST', body: form })
  },
  processBook: (id: string, pages?: number[]) =>
    request<{ started: boolean }>(`/books/${encodeURIComponent(id)}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pages === undefined ? {} : { pages }),
    }),
  pauseBook: (id: string) =>
    request<{ requested: boolean }>(`/books/${encodeURIComponent(id)}/pause`, {
      method: 'POST',
    }),
  addPages: (id: string, pages: number[]) =>
    request<BookDetail>(`/books/${encodeURIComponent(id)}/pages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages }),
    }),
  removePage: (id: string, pageNumber: number) =>
    request<BookDetail>(`/books/${encodeURIComponent(id)}/pages/${encodeURIComponent(pageNumber)}`, { method: 'DELETE' }),
  savePage: (bookId: string, pageNumber: number, page: PageDraft) =>
    request<Page>(
      `/books/${encodeURIComponent(bookId)}/pages/${encodeURIComponent(pageNumber)}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(page),
      },
    ),
  getSettings: () => request<Settings>('/settings'),
  saveSettings: (settings: Settings, clearApiKey: boolean) =>
    request<Settings>('/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_protocol: settings.api_protocol,
        base_url: settings.base_url,
        models_path: settings.models_path,
        responses_path: settings.responses_path,
        extraction_model: settings.extraction_model,
        reasoning_effort: settings.reasoning_effort,
        classification_model: settings.classification_model,
        api_key: settings.api_key || '',
        structured_output: false,
        timeout_seconds: settings.timeout_seconds,
        processing_concurrency: settings.processing_concurrency,
        context_reuse_enabled: settings.context_reuse_enabled,
        context_reuse_max_pages: settings.context_reuse_max_pages,
        clear_api_key: clearApiKey,
      }),
    }),
  fetchModels: (settings: Settings, clearApiKey: boolean) =>
    request<{ models: string[] }>('/settings/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_protocol: settings.api_protocol,
        base_url: settings.base_url,
        models_path: settings.models_path,
        api_key: settings.api_key || '',
        clear_api_key: clearApiKey,
        timeout_seconds: settings.timeout_seconds,
      }),
    }),
  testSettings: () => request<{ ok: boolean; message: string }>('/settings/test', { method: 'POST' }),
  exportBook: (id: string) => request<BookDetail>(`/books/${encodeURIComponent(id)}/export`),
}
