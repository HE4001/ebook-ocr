import type { Arrangement, Book, BookDetail, ExportManifest, ExportManifestCreate, Issue, LayoutCalibrationUpdate, LayoutSettings, OutputSnapshot, Page, PageContent, PageDraft, PageOutcomeSummary, PageResult, PaperSize, PdfCompileResult, RenderStrategy, Run, RunCreate, RunSummary, SelectionDraft, SelectionUpdate, Settings, SourcePageSummary } from './types'

export type ImportError = { filename: string; reason: string }
export type ImportResult = Arrangement & { import_errors?: ImportError[] }
export type SourcePageList = { items: SourcePageSummary[]; total: number; offset: number; limit: number }
export type OutputPageError = { page_id: string; position: number; source_filename: string; source_page: number; source_version: number; revision_id: string | null; reason: string }
export type OutputErrors = { pdf: OutputPageError[]; latex: OutputPageError[] }

const API_PREFIX = '/api'
export class ApiError extends Error {
  constructor(message: string, public readonly status?: number) { super(message); this.name = 'ApiError' }
}

function detailMessage(value: unknown, fallback: string): string {
  if (typeof value === 'object' && value && 'detail' in value) {
    const detail = (value as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit, responseFormat: 'json' | 'response' = 'json'): Promise<T> {
  const requestPath = `${API_PREFIX}${path}`
  let response: Response
  try {
    response = await fetch(requestPath, init)
  } catch (error) {
    if (init?.signal?.aborted) throw error
    throw new ApiError('无法连接本地服务，请确认后端已启动。')
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
    throw new ApiError(`本地请求失败（HTTP ${response.status}）${suffix}`, response.status)
  }
  if (response.status === 204) return undefined as T
  if (responseFormat === 'response') return response as T
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
    return request<ImportResult>(`/books/${encodeURIComponent(id)}/files`, { method: 'POST', body: form })
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
  compiledPageUrl: (pdfUrl: string, outputPage: number) => pdfUrl.replace(/\.pdf$/, `/pages/${outputPage}.png`),
  getBook: (id: string) => request<BookDetail>(`/books/${encodeURIComponent(id)}`),
  getOverview: (id: string, signal?: AbortSignal) => request<BookDetail>(`/books/${encodeURIComponent(id)}/overview`, { signal }),
  getSelection: (id: string, signal?: AbortSignal) => request<SelectionDraft>(`/books/${encodeURIComponent(id)}/selection`, { signal }),
  saveSelection: (id: string, value: SelectionUpdate) => request<SelectionDraft>(`/books/${encodeURIComponent(id)}/selection`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  getSourcePages: (id: string, offset = 0, limit = 100, sourceId?: string, signal?: AbortSignal) => request<SourcePageList>(`/books/${encodeURIComponent(id)}/source-pages?offset=${offset}&limit=${limit}${sourceId ? `&source_id=${encodeURIComponent(sourceId)}` : ''}`, { signal }),
  sourcePreviewUrl: (id: string, pageId: string, runId?: string, sourceVersion?: number) => `${API_PREFIX}/books/${encodeURIComponent(id)}/source-pages/${encodeURIComponent(pageId)}/preview?${new URLSearchParams({ ...(runId ? { run_id: runId } : {}), ...(sourceVersion !== undefined ? { source_version: String(sourceVersion) } : {}) })}`,
  listRunSummaries: (id: string, signal?: AbortSignal) => request<RunSummary[]>(`/books/${encodeURIComponent(id)}/runs/summaries`, { signal }),
  getRunSummary: (id: string, runId: string, signal?: AbortSignal) => request<RunSummary>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/summary`, { signal }),
  getRunOutcomes: (id: string, runId: string, offset = 0, limit = 50, signal?: AbortSignal) => request<PageOutcomeSummary[]>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/outcomes?offset=${offset}&limit=${limit}`, { signal }),
  getRunContent: (id: string, runId: string, pageId: string, signal?: AbortSignal) => request<PageContent | null>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/pages/${encodeURIComponent(pageId)}/content`, { signal }),
  getOutputSnapshot: (id: string, snapshotId: string, signal?: AbortSignal) => request<OutputSnapshot>(`/books/${encodeURIComponent(id)}/output-snapshots/${encodeURIComponent(snapshotId)}`, { signal }),
  getOutputErrors: (id: string, snapshotId: string, signal?: AbortSignal) => request<OutputErrors>(`/books/${encodeURIComponent(id)}/output-snapshots/${encodeURIComponent(snapshotId)}/errors`, { signal }),
  snapshotDownloadUrl: (id: string, snapshotId: string, format: 'json' | 'pdf' | 'latex') => `${API_PREFIX}/books/${encodeURIComponent(id)}/output-snapshots/${encodeURIComponent(snapshotId)}/${format}`,
  createRun: (id: string, run: RunCreate) => request<Run>(`/books/${encodeURIComponent(id)}/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(run),
  }),
  listRuns: (id: string, signal?: AbortSignal) => request<Run[]>(`/books/${encodeURIComponent(id)}/runs`, { signal }),
  getRun: (id: string, runId: string, signal?: AbortSignal) => request<Run>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`, { signal }),
  pauseRun: (id: string, runId: string) => request<Run>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/pause`, { method: 'POST' }),
  resumeRun: (id: string, runId: string) => request<Run>(`/books/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/resume`, { method: 'POST' }),
  getResults: (id: string, offset: number, limit: number, signal?: AbortSignal) => request<PageResult[]>(`/books/${encodeURIComponent(id)}/results?offset=${offset}&limit=${limit}`, { signal }),
  getIssues: (id: string, offset: number, limit: number, signal?: AbortSignal) => request<Issue[]>(`/books/${encodeURIComponent(id)}/issues?offset=${offset}&limit=${limit}`, { signal }),
  createExportManifest: (id: string, manifest: ExportManifestCreate) => request<ExportManifest>(`/books/${encodeURIComponent(id)}/export-manifests`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(manifest),
  }),
  getExportManifest: (id: string, manifestId: string, signal?: AbortSignal) => request<ExportManifest>(`/books/${encodeURIComponent(id)}/export-manifests/${encodeURIComponent(manifestId)}`, { signal }),
  manifestDownloadUrl: (id: string, manifestId: string, format: 'pdf' | 'partial_pdf' | 'latex' | 'json') => `${API_PREFIX}/books/${encodeURIComponent(id)}/export-manifests/${encodeURIComponent(manifestId)}/${format}`,
  downloadManifest: async (id: string, manifestId: string, format: 'pdf' | 'partial_pdf' | 'latex' | 'json', signal?: AbortSignal) => {
    const response = await request<Response>(`/books/${encodeURIComponent(id)}/export-manifests/${encodeURIComponent(manifestId)}/${format}`, { signal }, 'response')
    const extension = format === 'latex' ? '.zip' : format === 'json' ? '.json' : '.pdf'
    return { blob: await response.blob(), extension }
  },
  saveBookLayout: (id: string, changes: { paper_size?: PaperSize; layout?: LayoutSettings; render_strategy?: RenderStrategy }) => request<Book>(`/books/${encodeURIComponent(id)}/layout`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  }),
  compileBook: (id: string, printVersion: boolean) => request<PdfCompileResult>(`/books/${encodeURIComponent(id)}/compile?print_version=${printVersion}`, { method: 'POST' }),
  compilePage: (id: string, number: number, draft: PageDraft, printVersion: boolean) => request<PdfCompileResult>(`/books/${encodeURIComponent(id)}/pages/${number}/compile?print_version=${printVersion}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
  }),
  savePageLayout: (id: string, number: number, draft: LayoutCalibrationUpdate) => request<Page>(`/books/${encodeURIComponent(id)}/pages/${number}/layout`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft),
  }),
  compilePageLayout: (id: string, number: number, draft: LayoutCalibrationUpdate, printVersion: boolean) => request<PdfCompileResult>(`/books/${encodeURIComponent(id)}/pages/${number}/compile-layout-pdf?print_version=${printVersion}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft),
  }),
  exportLatex: async (id: string, printVersion: boolean) => {
    const response = await request<Response>(`/books/${encodeURIComponent(id)}/export.tex?print_version=${printVersion}`, undefined, 'response')
    const extension = response.headers.get('Content-Type')?.startsWith('application/zip') ? '.zip' : '.tex'
    return { blob: await response.blob(), extension }
  },
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
