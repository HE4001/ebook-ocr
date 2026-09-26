import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { BookContent, PageContent } from './Markdown'
import { buildStandaloneHtml, downloadText, safeFilename } from './exportHtml'
import ProjectOrganizer from './ProjectOrganizer'
import { PageEditor } from './PageEditor'
import { ReasoningControl } from './ReasoningControl'
import { bindingPageSide, PAPER_SIZES } from './paper'
import type { ApiProtocol, Book, BookDetail, Notice, Page, PageDraft, PaperSize, Settings, Usage } from './types'

const STATUS_LABEL: Record<string, string> = {
  uploaded: '待处理', processing: '处理中', pausing: '正在暂停', paused: '已暂停', ready: '已完成',
  failed: '失败', interrupted: '已中断',
}

const DEFAULT_API_BASES: Record<ApiProtocol, string> = {
  openai_responses: 'https://api.openai.com/v1',
  gemini: 'https://generativelanguage.googleapis.com/v1beta',
}

const EMPTY_SETTINGS: Settings = {
  api_protocol: 'openai_responses',
  base_url: DEFAULT_API_BASES.openai_responses,
  models_path: '/models',
  responses_path: '/responses',
  extraction_model: '',
  reasoning_effort: '',
  classification_model: '',
  api_key: '',
  has_api_key: false,
  structured_output: false,
  timeout_seconds: 120,
  processing_concurrency: 10,
  context_reuse_enabled: false,
  context_reuse_max_pages: 10,
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误'
}

function statusClass(status: string): string {
  if (status === 'ready') return 'status success'
  if (status === 'failed' || status === 'interrupted') return 'status danger'
  if (status === 'processing' || status === 'pausing') return 'status working'
  return 'status'
}

function endpoint(baseUrl: string, pathValue: string): string {
  const base = baseUrl.trim().replace(/\/+$/, '')
  const path = pathValue.trim()
  return path ? base + '/' + path.replace(/^\/+/, '') : base
}

function sameSavedSettings(current: Settings, saved: Settings): boolean {
  return current.api_protocol === saved.api_protocol
    && current.base_url === saved.base_url
    && current.models_path === saved.models_path
    && current.responses_path === saved.responses_path
    && current.extraction_model === saved.extraction_model
    && current.reasoning_effort === saved.reasoning_effort
    && current.timeout_seconds === saved.timeout_seconds
    && current.processing_concurrency === saved.processing_concurrency
    && current.context_reuse_enabled === saved.context_reuse_enabled
    && current.context_reuse_max_pages === saved.context_reuse_max_pages
}

function geminiGenerationEndpoint(settings: Settings): string {
  const collection = endpoint(settings.base_url, settings.models_path).replace(/\/+$/, '')
  const model = settings.extraction_model.trim().replace(/^models\//, '')
  return `${collection}/${model ? encodeURIComponent(model) : '{model}'}:generateContent`
}

function connectionTestMessage(result: { ok: boolean; message: string }, protocol: ApiProtocol) {
  if (!result.ok && /HTTP\s+404\b/i.test(result.message)) {
    const pathName = protocol === 'gemini' ? 'Gemini Models 资源集合路径' : 'Responses 接入路径'
    return { ...result, message: `模型服务返回 HTTP 404：请检查实际 POST 地址、${pathName}和模型名称。这不能证明 API 密钥有误。` }
  }
  return result
}

function UsageView({ usage, attempts }: { usage: Usage; attempts?: number }) {
  const count = (value: number | null) => value == null ? '未知' : value.toLocaleString()
  const known = [usage.input_tokens, usage.output_tokens, usage.total_tokens].some((value) => value != null)
  return (
    <div className="usage" aria-label="模型 token 用量">
      <span>输入 <strong>{count(usage.input_tokens)}</strong></span>
      <span>输出 <strong>{count(usage.output_tokens)}</strong></span>
      <span>合计 <strong>{count(usage.total_tokens)}</strong></span>
      {attempts !== undefined && <span>尝试 {attempts} 次</span>}
      {!usage.complete && <em>{known ? '已知用量，可能不完整' : '用量未知'}</em>}
    </div>
  )
}

function ToolbarUsage({ usage }: { usage: Usage }) {
  return <details className="toolbar-usage">
    <summary>模型用量 <strong>{usage.total_tokens == null ? '未知' : usage.total_tokens.toLocaleString()}</strong>{usage.total_tokens != null && ' tokens'}{!usage.complete && <span> · 不完整</span>}</summary>
    <UsageView usage={usage} />
  </details>
}

function SettingsView({ onNotice }: { onNotice: (notice: Notice) => void }) {
  const [settings, setSettings] = useState<Settings>(EMPTY_SETTINGS)
  const [savedSettings, setSavedSettings] = useState<Settings | null>(null)
  const [processingConcurrency, setProcessingConcurrency] = useState(String(EMPTY_SETTINGS.processing_concurrency))
  const [concurrencyError, setConcurrencyError] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [models, setModels] = useState<string[] | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState<string | null>(null)
  const modelsRequest = useRef(0)

  useEffect(() => {
    api.getSettings()
      .then((value) => {
        const loaded = {
          ...value,
          api_protocol: value.api_protocol ?? 'openai_responses',
          models_path: value.models_path ?? '/models',
          reasoning_effort: value.reasoning_effort ?? '',
          processing_concurrency: value.processing_concurrency ?? 10,
          context_reuse_enabled: value.context_reuse_enabled ?? false,
          context_reuse_max_pages: value.context_reuse_max_pages ?? 10,
          api_key: '',
        }
        setSettings(loaded)
        setSavedSettings(loaded)
        setProcessingConcurrency(String(loaded.processing_concurrency))
      })
      .catch((error) => onNotice({ kind: 'error', text: errorText(error) }))
      .finally(() => setLoading(false))
    return () => { modelsRequest.current += 1 }
  }, [onNotice])

  const resetModels = () => {
    modelsRequest.current += 1
    setModels(null)
    setModelsLoading(false)
    setModelsError(null)
  }

  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    if (key === 'api_protocol' || key === 'base_url' || key === 'api_key' || key === 'models_path') resetModels()
    setSettings((current) => ({ ...current, [key]: value }))
    setTestResult(null)
  }

  const changeProtocol = (protocol: ApiProtocol) => {
    resetModels()
    setTestResult(null)
    setSettings((current) => ({
      ...current,
      api_protocol: protocol,
      base_url: endpoint(current.base_url, '') === DEFAULT_API_BASES[current.api_protocol]
        ? DEFAULT_API_BASES[protocol]
        : current.base_url,
      extraction_model: '',
      classification_model: '',
      reasoning_effort: '',
      api_key: '',
    }))
  }

  const hasUnsavedChanges = savedSettings !== null && (
    !sameSavedSettings(settings, savedSettings)
    || processingConcurrency !== String(savedSettings.processing_concurrency)
    || Boolean(settings.api_key?.trim())
    || clearKey
  )

  const isGemini = settings.api_protocol === 'gemini'
  const sameApiConnection = savedSettings !== null
    && settings.api_protocol === savedSettings.api_protocol
    && endpoint(settings.base_url, '') === endpoint(savedSettings.base_url, '')
  const canReuseSavedKey = sameApiConnection && Boolean(savedSettings?.has_api_key)
  const needsNewKey = Boolean(savedSettings?.has_api_key)
    && !sameApiConnection && !settings.api_key?.trim() && !clearKey
  const modelLookupIssue = !settings.base_url.trim()
    ? '请先填写 API 根地址。'
    : clearKey
      ? '已勾选清除密钥，请取消勾选并填写或复用密钥后获取模型。'
      : !settings.api_key?.trim() && !canReuseSavedKey
        ? savedSettings?.has_api_key && !sameApiConnection
          ? '接入协议或 API 根地址已更改，请输入当前接入的密钥后获取模型。'
          : '请先输入 API 密钥，再获取模型。'
        : null

  const fetchModels = async () => {
    const requestId = ++modelsRequest.current
    setModelsLoading(true)
    setModels(null)
    setModelsError(null)
    try {
      const result = await api.fetchModels(settings, clearKey)
      if (requestId === modelsRequest.current) setModels(result.models)
    } catch (error) {
      if (requestId === modelsRequest.current) setModelsError(errorText(error))
    } finally {
      if (requestId === modelsRequest.current) setModelsLoading(false)
    }
  }

  const save = async () => {
    const concurrency = Number(processingConcurrency)
    if (!processingConcurrency.trim() || !Number.isInteger(concurrency) || concurrency < 1) {
      setConcurrencyError('请输入大于或等于 1 的整数。')
      return
    }
    setConcurrencyError('')
    setSaving(true)
    setTestResult(null)
    try {
      const saved = await api.saveSettings({ ...settings, processing_concurrency: concurrency }, clearKey)
      const loaded = {
        ...saved,
        api_protocol: saved.api_protocol ?? 'openai_responses',
        models_path: saved.models_path ?? '/models',
        reasoning_effort: saved.reasoning_effort ?? '',
        processing_concurrency: saved.processing_concurrency ?? 10,
        context_reuse_enabled: saved.context_reuse_enabled ?? false,
        context_reuse_max_pages: saved.context_reuse_max_pages ?? 10,
        api_key: '',
      }
      setSettings(loaded)
      setSavedSettings(loaded)
      setProcessingConcurrency(String(loaded.processing_concurrency))
      setClearKey(false)
      resetModels()
      onNotice({
        kind: 'success',
        text: clearKey
          ? '设置已保存。本机已保存的 API 密钥已清除。'
          : saved.has_api_key
            ? '设置已保存。API 密钥已保存到本机，重启后自动读取。'
            : '设置已保存。本机尚未保存 API 密钥。',
      })
    } catch (error) {
      onNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    setSaving(true)
    setTestResult(null)
    try {
      setTestResult(connectionTestMessage(await api.testSettings(), settings.api_protocol))
    } catch (error) {
      setTestResult({ ok: false, message: errorText(error) })
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="center-state">正在读取设置…</div>
  return (
    <main className="settings-page">
      <header className="section-heading"><p className="eyebrow">模型接入</p><h1>设置</h1></header>
      <div className="settings-card">
        <label className="protocol-field"><span>接入协议</span><select value={settings.api_protocol} onChange={(event) => changeProtocol(event.target.value as ApiProtocol)} disabled={saving} aria-describedby="api-protocol-help"><option value="openai_responses">OpenAI Responses</option><option value="gemini">Google Gemini 原生</option></select><small id="api-protocol-help">按服务提供的接入方式选择。切换协议会清空模型选择、推理程度和未保存密钥。</small></label>
        <label><span>API 根地址</span><input value={settings.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder={DEFAULT_API_BASES[settings.api_protocol]} disabled={saving} /><small>填写包含协议和 API 版本的地址；自定义接口路径可在下方高级项中设置。</small></label>
        <label><span className="field-title">API 密钥 <span className={canReuseSavedKey ? 'key-state saved' : 'key-state missing'}>{canReuseSavedKey ? '已保存到本机' : savedSettings?.has_api_key ? '当前接入未配置' : '未配置'}</span></span><input type="password" autoComplete="new-password" value={settings.api_key ?? ''} onChange={(event) => update('api_key', event.target.value)} placeholder={canReuseSavedKey ? '已保存；留空保留' : '输入当前接入的密钥'} disabled={saving || clearKey} aria-describedby={needsNewKey ? 'api-key-help api-key-warning' : 'api-key-help'} /><small id="api-key-help">已保存密钥不会回显；仅协议和 API 根地址均相同时，留空可复用。输入新密钥会替换已保存密钥。密钥以明文保存在本机 SQLite 数据库中，不保存在浏览器中。</small>{needsNewKey && <small id="api-key-warning" className="field-error" role="status">接入协议或 API 根地址已更改，请输入当前接入的密钥，或勾选清除已保存密钥后再保存。</small>}</label>
        <label className="check-row"><input type="checkbox" checked={clearKey} disabled={saving} onChange={(event) => { const checked = event.target.checked; setClearKey(checked); resetModels(); setTestResult(null); if (checked) setSettings((current) => ({ ...current, api_key: '' })) }} /><span>清除本机已保存的密钥</span></label>
        <details className="settings-advanced">
          <summary>高级接入路径</summary>
          <div className="settings-paths">
            <div>
              <label><span>{isGemini ? 'Models 资源集合路径' : 'Models 路径'}</span><input value={settings.models_path} onChange={(event) => update('models_path', event.target.value)} placeholder="/models" disabled={saving} /><small>{isGemini ? '默认 /models；获取列表和生成内容共用此集合。留空时，根地址须为完整集合地址。' : '默认 /models；可填自定义相对路径，留空则直接请求根地址。'}</small></label>
              <div className="endpoint-preview"><span>GET Models</span><code>{endpoint(settings.base_url, settings.models_path)}</code></div>
            </div>
            {isGemini ? <div>
              <span className="path-preview-title">内容生成地址</span>
              <small>在资源集合地址后追加模型 ID 和 :generateContent；模型 ID 会移除一次 models/ 前缀并编码。</small>
              <div className="endpoint-preview"><span>POST generateContent</span><code>{geminiGenerationEndpoint(settings)}</code>{!settings.extraction_model.trim() && <small>选择或填写模型后，地址中的 {'{model}'} 会替换为模型 ID。</small>}</div>
            </div> : <div>
              <label><span>Responses 路径</span><input value={settings.responses_path} onChange={(event) => update('responses_path', event.target.value)} placeholder="/responses" disabled={saving} /><small>默认 /responses；可填自定义相对路径，留空则直接请求根地址。</small></label>
              <div className="endpoint-preview"><span>POST Responses</span><code>{endpoint(settings.base_url, settings.responses_path)}</code></div>
            </div>}
          </div>
        </details>
        <div className="model-discovery">
          <div className="model-fetch-row"><button type="button" onClick={fetchModels} disabled={saving || modelsLoading || Boolean(modelLookupIssue)}>{modelsLoading ? '正在获取…' : '获取模型'}</button><small>使用当前地址与密钥，无需先保存。</small></div>
          <div className="model-lookup-status" aria-live="polite">
            {modelsError
              ? <p className="model-lookup-error" role="alert">获取失败：{modelsError} 请检查地址、Models 路径和密钥后重试，也可手动填写模型 ID。</p>
              : <p>{modelLookupIssue || (modelsLoading ? '正在向服务器获取模型列表…' : models === null ? '点击获取服务器的模型名称，或直接手动填写模型 ID。' : models.length ? `已获取 ${models.length} 个模型。请选择需要使用的模型。` : isGemini ? '服务器未返回声明支持 generateContent 的模型。可检查资源集合路径后重试，或手动填写模型 ID。' : '服务器返回的模型列表为空。可检查 Models 路径后重试，或手动填写模型 ID。')}</p>}
          </div>
          <label><span>服务器模型</span><select value={models?.includes(settings.extraction_model) ? settings.extraction_model : ''} onChange={(event) => update('extraction_model', event.target.value)} disabled={saving || modelsLoading || !models?.length}><option value="" disabled>{models?.length ? '选择模型，不会自动保存' : '获取模型后选择'}</option>{models?.map((model) => <option key={model} value={model}>{model}</option>)}</select></label>
          <small>{isGemini ? '仅列出服务声明支持 generateContent 的模型；不保证支持图片输入或结构化输出，请确认所选模型能力。' : '模型列表仅提供名称，不代表模型支持 Responses 或图片输入，请向供应商确认。'}</small>
        </div>
        <label><span>页面代理模型 ID</span><input value={settings.extraction_model} onChange={(event) => update('extraction_model', event.target.value)} placeholder="手动填写，或从上方列表选择" disabled={saving} /><small>{isGemini && '可填裸模型 ID 或 models/ 开头的完整资源名。'}保存后用于逐页识别，返回页眉、正文 Markdown 和页脚。</small></label>
        <ReasoningControl protocol={settings.api_protocol} value={settings.reasoning_effort} onChange={(value) => update('reasoning_effort', value)} disabled={saving} />
        <div className="field-grid compact-grid">
          <label><span>超时秒数</span><input type="number" min={5} max={600} value={settings.timeout_seconds} onChange={(event) => update('timeout_seconds', Number(event.target.value))} disabled={saving} /></label>
        </div>
        <label className="number-field">
          <span>处理并发数</span>
          <input type="number" min={1} step={1} value={processingConcurrency} onChange={(event) => { setProcessingConcurrency(event.target.value); setConcurrencyError(''); setTestResult(null) }} disabled={saving} aria-invalid={Boolean(concurrencyError)} aria-describedby={concurrencyError ? 'processing-concurrency-help processing-concurrency-error' : 'processing-concurrency-help'} />
          <small id="processing-concurrency-help">每个项目同时识别的最多页数，默认 10。请输入正整数，无固定上限。</small>
          {concurrencyError && <small id="processing-concurrency-error" className="field-error" role="alert">{concurrencyError}</small>}
        </label>
        <label className="check-row">
          <input type="checkbox" checked={settings.context_reuse_enabled} onChange={(event) => update('context_reuse_enabled', event.target.checked)} disabled={saving} aria-describedby="context-reuse-help" />
          <span>上下文复用（实验性）<small id="context-reuse-help">{isGemini ? '本机会保留同组页面的识别历史，每次请求重传完整历史和图片；历史内容仍占用上下文和用量。' : '让相邻页面共享识别上下文。需要模型服务支持保存并续接对话，历史内容仍占用上下文和用量。'}</small></span>
        </label>
        {settings.context_reuse_enabled && <label className="number-field">
          <span>每条对话最多识别页数</span>
          <select value={settings.context_reuse_max_pages} onChange={(event) => update('context_reuse_max_pages', Number(event.target.value))} disabled={saving} aria-describedby="context-reuse-pages-help">
            {Array.from({ length: 10 }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} 页</option>)}
          </select>
          <small id="context-reuse-pages-help">含首张页面。按编排顺序连续分组，组间并行、组内依次识别，最终页序不变；每次开始处理都会建立新对话。</small>
        </label>}
        {testResult && <div className={testResult.ok ? 'inline-result success-box' : 'inline-result error-box'}>{testResult.message}</div>}
        <div className="button-row"><button className="primary" onClick={save} disabled={saving || modelsLoading || needsNewKey}>{saving ? '请稍候…' : '保存设置'}</button><button onClick={test} disabled={saving || modelsLoading || hasUnsavedChanges} title={hasUnsavedChanges ? '请先保存当前改动' : undefined}>主动测试连接</button></div>
        {hasUnsavedChanges && <p className="settings-warning">有未保存改动。请先保存，再测试模型连接。</p>}
        <p className="settings-footnote">获取模型只查询列表，不保存设置或发送推理请求。保存设置不会联系模型服务。主动测试使用已保存配置发送请求，可能产生供应商用量。</p>
      </div>
    </main>
  )
}

type WorkspaceView = 'upload' | 'organize' | 'workspace' | 'preview' | 'settings'

function projectView(book: Book): WorkspaceView {
  if (!book.upload_confirmed) return 'upload'
  return book.selection_confirmed ? 'workspace' : 'organize'
}

function projectStatus(book: Book): string {
  if (!book.upload_confirmed) return '待确认资料'
  if (!book.selection_confirmed) return '待编排'
  return STATUS_LABEL[book.status] ?? book.status
}

function sourceLabel(page: Page): string {
  return `${page.source_filename} · 第 ${page.source_page} 页`
}

function draftFromPage(page: Page | null): PageDraft {
  return page ? { text: page.text, page_kind: page.page_kind, cover_fields: page.cover_fields }
    : { text: '', page_kind: 'content', cover_fields: [] }
}

function PaperSizeControl({ value, saving, disabled, onChange }: {
  value: PaperSize
  saving: boolean
  disabled: boolean
  onChange: (size: PaperSize) => void
}) {
  return <div className="book-paper-control">
    <span className="paper-swatch" aria-hidden="true" style={{ aspectRatio: `${PAPER_SIZES[value].widthMm} / ${PAPER_SIZES[value].heightMm}` }}><i /><i /><i /></span>
    <div className="paper-field">
      <label htmlFor="book-paper-size">成书纸张</label>
      <select id="book-paper-size" value={value} disabled={disabled} aria-describedby="book-paper-note" onChange={(event) => onChange(event.target.value as PaperSize)}>
        {Object.entries(PAPER_SIZES).map(([size, paper]) => <option key={size} value={size}>{paper.label}</option>)}
      </select>
      <small id="book-paper-note" aria-live="polite">{saving ? '正在保存…' : '整书生效 · 选择后自动保存'}</small>
    </div>
  </div>
}

export default function App() {
  const [view, setView] = useState<WorkspaceView>('upload')
  const [books, setBooks] = useState<Book[]>([])
  const [selectedBookId, setSelectedBookId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BookDetail | null>(null)
  const [selectedPageNumber, setSelectedPageNumber] = useState(1)
  const [pageDraft, setPageDraft] = useState<PageDraft>(() => draftFromPage(null))
  const [dirty, setDirty] = useState(false)
  const [organizerDirty, setOrganizerDirty] = useState(false)
  const [organizerBusy, setOrganizerBusy] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [initialLoading, setInitialLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [layoutSaving, setLayoutSaving] = useState(false)
  const [printVersion, setPrintVersion] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [newProject, setNewProject] = useState(false)
  const [projectTitle, setProjectTitle] = useState('')
  const [uploadError, setUploadError] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const selectedBookIdRef = useRef<string | null>(null)
  const detailRequestRef = useRef(0)
  const showNotice = useCallback((value: Notice) => setNotice(value), [])

  useEffect(() => {
    if (!notice || notice.kind === 'error') return
    const timer = window.setTimeout(() => setNotice(null), 5000)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    if (!dirty && !organizerDirty) return
    const protectDraft = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', protectDraft)
    return () => window.removeEventListener('beforeunload', protectDraft)
  }, [dirty, organizerDirty])

  const loadBooks = useCallback(async () => {
    try {
      const value = await api.listBooks()
      setBooks(value)
      const nextId = selectedBookIdRef.current ?? value[0]?.id ?? null
      selectedBookIdRef.current = nextId
      setSelectedBookId(nextId)
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setInitialLoading(false)
    }
  }, [])

  const applyDetail = useCallback((value: BookDetail) => {
    if (value.book.id !== selectedBookIdRef.current) return
    setDetail(value)
    setBooks((current) => current.map((book) => book.id === value.book.id ? value.book : book))
    setSelectedPageNumber((current) => value.pages.some((page) => page.number === current)
      ? current : value.pages[0]?.number ?? 1)
  }, [])

  const loadDetail = useCallback(async (id: string, quiet = false, openProject = false) => {
    const requestNumber = ++detailRequestRef.current
    if (!quiet) setDetailLoading(true)
    try {
      const value = await api.getBook(id)
      if (id !== selectedBookIdRef.current || requestNumber !== detailRequestRef.current) return
      applyDetail(value)
      if (openProject) setView((current) => current === 'settings' ? current : projectView(value.book))
    } catch (error) {
      if (!quiet && id === selectedBookIdRef.current) setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      if (!quiet && id === selectedBookIdRef.current) setDetailLoading(false)
    }
  }, [applyDetail])

  useEffect(() => { void loadBooks() }, [loadBooks])
  useEffect(() => {
    setDirty(false)
    setOrganizerDirty(false)
    setUploadError('')
    setDetail(null)
    setSelectedPageNumber(1)
    setPrintVersion(false)
    if (selectedBookId) void loadDetail(selectedBookId, false, true)
  }, [selectedBookId, loadDetail])
  useEffect(() => {
    if (!selectedBookId || actionBusy || layoutSaving || !['processing', 'pausing'].includes(detail?.book.status ?? '')) return
    const timer = window.setInterval(() => void loadDetail(selectedBookId, true), 2000)
    return () => window.clearInterval(timer)
  }, [selectedBookId, detail?.book.status, actionBusy, layoutSaving, loadDetail])

  const sourcePage = useMemo(() => detail?.pages.find((page) => page.number === selectedPageNumber) ?? null, [detail, selectedPageNumber])
  const sourcePageHasResult = sourcePage && (sourcePage.status === 'ready' || Boolean(sourcePage.text || sourcePage.cover_fields.length || sourcePage.header_segments.length || sourcePage.footer_segments.length))
  const pendingPages = detail?.pages.filter((page) => page.status !== 'ready') ?? []
  const pagesToProcess = pendingPages.map((page) => page.number)
  const bindingPageCount = detail?.pages.filter((page) => bindingPageSide(page) !== 'unknown').length ?? 0
  const effectivePrintVersion = printVersion && bindingPageCount > 0
  const isRunning = detail?.book.status === 'processing' || detail?.book.status === 'pausing'
  const busy = actionBusy || saving || layoutSaving || uploading || deleting || organizerBusy
  const processLocked = isRunning || busy
  const editLocked = busy || sourcePage?.status === 'processing'
  const progress = detail?.book.selected_page_count ? Math.round(detail.book.completed_pages / detail.book.selected_page_count * 100) : 0
  const oldBackendContract = Boolean(detail && (!Array.isArray(detail.files) || !detail.book.usage
    || detail.pages.some((page) => typeof page.text !== 'string' || !page.page_kind || !Array.isArray(page.cover_fields) || !Array.isArray(page.header_segments) || !Array.isArray(page.footer_segments) || !page.usage)))

  useEffect(() => { if (!dirty) setPageDraft(draftFromPage(sourcePage)) }, [sourcePage, dirty])

  const leaveDrafts = () => {
    const message = organizerDirty ? '页面编排尚未确认，确定离开并放弃这些调整吗？' : '当前页有未保存修改，确定离开并放弃吗？'
    if ((dirty || organizerDirty) && !window.confirm(message)) return false
    setDirty(false)
    setOrganizerDirty(false)
    return true
  }

  const changeView = (next: WorkspaceView) => {
    if (busy || (next === view && !newProject)) return
    if (!leaveDrafts()) return
    setNewProject(false)
    setView(next)
  }

  const choosePage = (page: Page) => {
    if (busy || page.number === selectedPageNumber) return
    if (!leaveDrafts()) return
    setSelectedPageNumber(page.number)
    setPageDraft(draftFromPage(page))
  }

  const chooseBook = (id: string) => {
    if (busy) return
    if (id === selectedBookId) {
      if (newProject) setNewProject(false)
      return
    }
    if (!leaveDrafts()) return
    setNewProject(false)
    selectedBookIdRef.current = id
    setSelectedBookId(id)
  }

  const beginProject = () => {
    if (busy || !leaveDrafts()) return
    setProjectTitle('')
    setNewProject(true)
    setView('upload')
  }

  const createProject = async () => {
    if (!projectTitle.trim() || busy) return
    setActionBusy(true)
    try {
      const book = await api.createProject(projectTitle.trim())
      setBooks((current) => [book, ...current])
      selectedBookIdRef.current = book.id
      setSelectedBookId(book.id)
      setNewProject(false)
      setView('upload')
      setNotice({ kind: 'success', text: '项目已创建。请向项目中添加 PDF 或图片。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setActionBusy(false)
    }
  }

  const upload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    if (!detail || processLocked || files.length === 0) return
    setUploading(true)
    setUploadError('')
    const id = detail.book.id
    setNotice({ kind: 'info', text: `正在向项目添加 ${files.length} 个文件，并读取页面信息…` })
    try {
      const value = await api.uploadFiles(id, files)
      applyDetail({ book: value.book, files: value.files, pages: value.order.map((number) => value.pages.find((page) => page.number === number)!) })
      setView('upload')
      setNotice({ kind: 'success', text: `已添加 ${files.length} 个文件。项目现有 ${value.files.length} 个文件，请确认资料后继续编排。` })
    } catch (error) {
      const message = errorText(error)
      setUploadError(message)
      setNotice({ kind: 'error', text: message })
      await loadDetail(id, true)
    } finally {
      setUploading(false)
    }
  }

  const confirmUpload = async () => {
    if (!detail || processLocked || detail.files.length === 0) return
    setActionBusy(true)
    try {
      const value = await api.confirmUpload(detail.book.id)
      applyDetail({ book: value.book, files: value.files, pages: value.order.map((number) => value.pages.find((page) => page.number === number)!) })
      setView('organize')
      setNotice({ kind: 'success', text: '资料已确认。请选择页面、调整文件和页内顺序，再确认最终编排。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setActionBusy(false)
    }
  }

  const confirmArrangement = (value: BookDetail) => {
    applyDetail(value)
    setOrganizerDirty(false)
    setView('workspace')
    setNotice({ kind: 'success', text: `编排已确认，共 ${value.pages.length} 页。可以开始识别并逐页校对。` })
  }

  const processPages = async (pages: number[]) => {
    if (!detail?.book.selection_confirmed || processLocked || pages.length === 0) return
    const bookId = detail.book.id
    const targets = detail.pages.filter((page) => pages.includes(page.number))
    const discardDraft = dirty && pages.includes(selectedPageNumber)
    if ((targets.some((page) => page.status === 'ready' || Boolean(page.text || page.cover_fields.length || page.header_segments.length || page.footer_segments.length)) || discardDraft)
      && !window.confirm(`这次处理会产生新的模型用量，成功后会覆盖本次处理页面已有的识别或校对文本。${discardDraft ? '当前未保存修改也会被放弃。' : ''}确定继续吗？`)) return
    setActionBusy(true)
    try {
      const result = await api.processBook(bookId, pages)
      if (result.started && discardDraft) {
        setDirty(false)
        setPageDraft(draftFromPage(sourcePage))
      }
      setNotice({ kind: 'success', text: result.started ? '处理已开始，页面完成后自动更新，最终顺序保持不变。' : '当前没有需要处理的页面。' })
      await loadDetail(bookId, true)
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setActionBusy(false)
    }
  }

  const pauseBook = async () => {
    if (!selectedBookId) return
    setActionBusy(true)
    try {
      await api.pauseBook(selectedBookId)
      setNotice({ kind: 'info', text: '已请求暂停。等待所有已开始的页面完成后暂停，不再开始新页面。' })
      await loadDetail(selectedBookId, true)
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
      await loadDetail(selectedBookId, true)
    } finally {
      setActionBusy(false)
    }
  }

  const deleteBook = async () => {
    if (!detail || processLocked) return
    const book = detail.book
    if (!window.confirm(`确定删除项目「${book.title}」吗？这会删除项目内全部源文件、识别和校对结果，且无法撤销。`)) return
    setDeleting(true)
    try {
      await api.deleteBook(book.id)
      const remaining = books.filter((item) => item.id !== book.id)
      setBooks(remaining)
      selectedBookIdRef.current = remaining[0]?.id ?? null
      setSelectedBookId(selectedBookIdRef.current)
      setDetail(null)
      setPageDraft(draftFromPage(null))
      setDirty(false)
      setOrganizerDirty(false)
      setView('upload')
      setNotice({ kind: 'success', text: `已删除项目「${book.title}」。` })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setDeleting(false)
    }
  }

  const savePaperSize = async (paperSize: PaperSize) => {
    if (!detail || layoutSaving || paperSize === (detail.book.paper_size ?? 'a4')) return
    setLayoutSaving(true)
    detailRequestRef.current += 1
    try {
      const saved = await api.saveBookLayout(detail.book.id, paperSize)
      setDetail((current) => current?.book.id === saved.id ? { ...current, book: { ...current.book, paper_size: saved.paper_size } } : current)
      setBooks((current) => current.map((book) => book.id === saved.id ? { ...book, paper_size: saved.paper_size } : book))
      await loadDetail(saved.id, true)
      setNotice({ kind: 'success', text: `已将整本书纸张设为 ${PAPER_SIZES[saved.paper_size].label}。` })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setLayoutSaving(false)
    }
  }

  const savePage = async () => {
    if (!detail || !sourcePage || editLocked) return
    setSaving(true)
    try {
      const saved = await api.savePage(detail.book.id, sourcePage.number, {
        ...pageDraft,
        text: pageDraft.page_kind === 'content' ? pageDraft.text : '',
        cover_fields: pageDraft.page_kind === 'content' ? [] : pageDraft.cover_fields.filter((field) => field.text.trim()),
      })
      setDetail((current) => current ? { ...current, pages: current.pages.map((page) => page.number === saved.number ? saved : page) } : current)
      setPageDraft(draftFromPage(saved))
      setDirty(false)
      await loadDetail(detail.book.id, true)
      setNotice({ kind: 'success', text: `${sourceLabel(saved)} 已保存。` })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setSaving(false)
    }
  }

  const exportData = async (kind: 'json' | 'html') => {
    if (!detail) return
    setActionBusy(true)
    try {
      const data = await api.exportBook(detail.book.id)
      const name = safeFilename(data.book.title)
      if (kind === 'json') downloadText(name + '.json', JSON.stringify(data, null, 2), 'application/json;charset=utf-8')
      if (kind === 'html') downloadText(name + '.html', buildStandaloneHtml(data, effectivePrintVersion && data.pages.some((page) => bindingPageSide(page) !== 'unknown')), 'text/html;charset=utf-8')
      setNotice({ kind: 'success', text: (kind === 'html' ? '独立 HTML' : 'JSON') + ' 已下载。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setActionBusy(false)
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar no-print">
        <button className="brand" onClick={() => changeView(detail ? projectView(detail.book) : 'upload')} disabled={busy}><span className="brand-mark">页</span><span>纸页重排</span></button>
        <nav aria-label="主导航">
          <button className={view !== 'settings' ? 'nav-active' : ''} onClick={() => changeView(detail ? projectView(detail.book) : 'upload')} disabled={busy}>项目工作台</button>
          <button className={view === 'settings' ? 'nav-active' : ''} onClick={() => changeView('settings')} disabled={busy}>设置</button>
        </nav>
      </header>
      {notice && <div className={'notice global-notice ' + notice.kind + ' no-print'} role="status"><span>{notice.text}</span><button onClick={() => setNotice(null)} aria-label="关闭提示">×</button></div>}
      {view === 'settings' ? <SettingsView onNotice={showNotice} /> : (
        <div className="layout">
          <aside className="library no-print">
            <div className="library-heading"><div><p className="eyebrow">本地工作空间</p><h2>项目</h2></div><button className="compact-button" onClick={beginProject} disabled={busy}>＋ 新建</button></div>
            {initialLoading ? <p className="sidebar-state">正在读取项目…</p> : books.length === 0 ? <div className="library-empty"><p>还没有项目</p><span>将同一本书的资料放在一起。</span></div> : (
              <div className="book-list">{books.map((book) => (
                <button className={book.id === selectedBookId && !newProject ? 'book-item selected' : 'book-item'} onClick={() => chooseBook(book.id)} disabled={busy} key={book.id} aria-current={book.id === selectedBookId && !newProject ? 'page' : undefined}>
                  <span className="book-item-title">{book.title}</span>
                  <span className="project-file-count">{book.file_count} 个文件 · {book.page_count} 页</span>
                  <span className="book-item-meta"><span className={statusClass(book.status)}>{projectStatus(book)}</span>{book.selection_confirmed && <span>{book.completed_pages} / {book.selected_page_count} 已完成</span>}</span>
                </button>
              ))}</div>
            )}
            <p className="library-footnote">资料保存在本机<br />PDF 与图片可在同一项目中混排</p>
          </aside>
          <main className="main-panel">
            {newProject ? (
              <section className="project-create">
                <p className="eyebrow">建立一本书的工作空间</p><h1>新建项目</h1><p>为这份书稿起个名字，再把 PDF、扫描图和补充页放进同一个项目。</p>
                <form onSubmit={(event) => { event.preventDefault(); void createProject() }}>
                  <label htmlFor="project-title">项目名称</label><input id="project-title" value={projectTitle} onChange={(event) => setProjectTitle(event.target.value)} maxLength={200} placeholder="例如：Python 学习手册" autoFocus disabled={actionBusy} />
                  <div className="button-row"><button className="primary" type="submit" disabled={!projectTitle.trim() || actionBusy}>{actionBusy ? '创建中…' : '创建项目并添加资料'}</button><button type="button" onClick={() => setNewProject(false)} disabled={actionBusy}>取消</button></div>
                </form>
              </section>
            ) : !selectedBookId ? (
              <div className="welcome-state"><span className="welcome-icon">页</span><h1>多份资料，一本书稿</h1><p>先建立项目，汇集 PDF 与图片；确认页序后，再识别和校对。</p><button className="primary" onClick={beginProject} disabled={busy}>新建第一个项目</button></div>
            ) : detailLoading || !detail || detail.book.id !== selectedBookId ? <div className="center-state">正在读取项目…</div>
              : oldBackendContract ? <div className="center-state"><h2>后端仍在运行旧版本</h2><p>请使用 stop.bat 和 start.bat 重启后端，以加载多文件项目接口。</p></div>
              : <>
                <header className="book-header project-header no-print">
                  <div className="book-overview">
                    <div className="book-heading">
                      <p className="eyebrow">当前项目</p><div className="book-title-row"><h1>{detail.book.title}</h1><span className={statusClass(detail.book.status)}>{projectStatus(detail.book)}</span></div>
                      <div className="book-meta"><p className="book-summary"><span>{detail.files.length} 个文件</span><span>源资料 {detail.book.page_count} 页</span>{detail.book.selection_confirmed && <><span>编排 {detail.book.selected_page_count} 页</span><span>已完成 {detail.book.completed_pages} 页</span></>}</p></div>
                    </div>
                    <button className="book-delete danger-action" onClick={deleteBook} disabled={processLocked}>{deleting ? '删除中…' : '删除项目'}</button>
                  </div>
                  <nav className="workflow-nav" aria-label="项目处理步骤">
                    <button className={view === 'upload' ? 'current' : ''} aria-current={view === 'upload' ? 'step' : undefined} onClick={() => changeView('upload')} disabled={busy}><span>1</span><strong>上传资料</strong><small>{detail.book.upload_confirmed ? '已确认' : '确认文件'}</small></button>
                    <button className={view === 'organize' ? 'current' : ''} aria-current={view === 'organize' ? 'step' : undefined} onClick={() => changeView('organize')} disabled={!detail.book.upload_confirmed || busy || isRunning}><span>2</span><strong>页面编排</strong><small>{detail.book.selection_confirmed ? '已确认' : '选页与排序'}</small></button>
                    <button className={view === 'workspace' ? 'current' : ''} aria-current={view === 'workspace' ? 'step' : undefined} onClick={() => changeView('workspace')} disabled={!detail.book.selection_confirmed || busy}><span>3</span><strong>逐页校对</strong><small>识别与编辑</small></button>
                    <button className={view === 'preview' ? 'current' : ''} aria-current={view === 'preview' ? 'step' : undefined} onClick={() => changeView('preview')} disabled={!detail.book.selection_confirmed || busy}><span>4</span><strong>整书预览</strong><small>预览与导出</small></button>
                  </nav>
                </header>
                {view === 'upload' ? (
                  <section className="project-upload">
                    <div className="upload-intro"><div><p className="eyebrow">项目资料</p><h2>{detail.files.length ? '确认本次使用的文件' : '把资料添加到这个项目'}</h2><p>支持一次选择多个 PDF、PNG 或 JPEG，可分批继续添加。</p></div><span className="file-total">{detail.files.length}<small>个文件</small></span></div>
                    <input ref={fileInput} className="visually-hidden" type="file" multiple accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" onChange={upload} disabled={processLocked} />
                    <div className="upload-target"><svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M8 11V5h12l5 5v17H8V21M20 5v6h5M3 16h13m-4-4 4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg><div><strong>{uploading ? '正在添加文件并读取页面…' : 'PDF 和图片，一起加入'}</strong><p>{isRunning ? '处理运行中；暂停后可添加资料。' : detail.book.selection_confirmed ? '添加新资料后，需要重新确认上传与编排。已有识别结果会保留。' : '上传完成后点击下方确认，再选择页面和编排顺序。'}</p></div><button className="primary" onClick={() => fileInput.current?.click()} disabled={processLocked}>{uploading ? '上传中…' : '选择文件'}</button></div>
                    {uploadError && <div className="upload-error" role="alert"><strong>文件添加未完成</strong><p>{uploadError}</p><span>下方列表显示当前项目已保存的文件，请核对后再继续添加。</span></div>}
                    {detail.files.length > 0 && <div className="source-groups">{(['pdf', 'image'] as const).map((kind) => {
                      const files = detail.files.filter((file) => file.kind === kind)
                      return files.length > 0 && <section className="source-group" key={kind}><div className="source-group-heading"><h3>{kind === 'pdf' ? 'PDF 文档' : '图片'}</h3><span>{files.length} 个文件 · {files.reduce((total, file) => total + file.page_count, 0)} 页</span></div><ul>{files.map((file) => <li key={file.id}><span className={'file-kind ' + kind}>{kind === 'pdf' ? 'PDF' : '图'}</span><span className="source-filename" title={file.filename}>{file.filename}</span><span className="source-pages">{file.page_count} 页</span></li>)}</ul></section>
                    })}</div>}
                    <div className="upload-confirm"><p>{detail.files.length ? `共 ${detail.files.length} 个文件、${detail.book.page_count} 页。确认后进入页面编排。` : '先添加至少一个文件，再确认资料。'}</p><button className="primary" onClick={confirmUpload} disabled={processLocked || detail.files.length === 0}>{actionBusy ? '确认中…' : detail.book.upload_confirmed ? '进入页面编排' : '确认上传，开始编排'}</button></div>
                  </section>
                ) : view === 'organize' ? <ProjectOrganizer key={detail.book.id} bookId={detail.book.id} onConfirmed={confirmArrangement} onNotice={showNotice} disabled={processLocked} onDirtyChange={setOrganizerDirty} onBusyChange={setOrganizerBusy} />
                  : view === 'preview' ? <>
                    <section className="preview-toolbar no-print" aria-labelledby="preview-title">
                      <div className="preview-heading">
                        <div><h2 id="preview-title">预览与导出</h2><p>{detail.pages.length} 页已编排 · 长正文按纸张自然分页</p></div>
                        <div className="button-row export-buttons">
                          <button onClick={() => void exportData('json')} disabled={busy}>下载 JSON</button>
                          <button onClick={() => void exportData('html')} disabled={busy}>下载 HTML</button>
                          <button className="primary" onClick={() => window.print()} disabled={busy}><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 8V3h10v5M7 17H4V9h16v8h-3M7 14h10v7H7zM17 11h.01" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>打印 / 存为 PDF</button>
                        </div>
                      </div>
                      <div className="preview-options">
                        <PaperSizeControl value={detail.book.paper_size ?? 'a4'} saving={layoutSaving} disabled={busy} onChange={savePaperSize} />
                        <div className="print-layout-option">
                          <span className={`binding-preview${effectivePrintVersion ? ' is-bound' : ''}`} aria-hidden="true"><i /><i /></span>
                          <label className="print-version-control">
                            <span><strong>打印装订版</strong><small id="print-version-note">{bindingPageCount === 0
                              ? '没有可用左右页信息，所有页面保持居中'
                              : `${bindingPageCount}/${detail.pages.length} 页可用；${effectivePrintVersion ? '已按左右页预留装订边距' : '开启后按左右页预留装订边距'}，其他页仍居中`}</small></span>
                            <input type="checkbox" role="switch" checked={effectivePrintVersion} disabled={busy || bindingPageCount === 0} onChange={(event) => setPrintVersion(event.target.checked)} aria-describedby="print-version-note" />
                          </label>
                        </div>
                      </div>
                      <div className="toolbar-footnotes">
                        <details className="export-help"><summary>导出与打印说明</summary><p>HTML 可独立离线阅读，公式显示取决于浏览器数学字体。打印时请选择与成书一致的纸张，并关闭浏览器页眉和页脚。</p><p>装订版中，无页脚、未知页侧和封面封底仍居中。长正文续页沿用源页版式，实际分页以打印预览为准。</p></details>
                        <ToolbarUsage usage={detail.book.usage} />
                      </div>
                    </section>
                    <BookContent detail={detail} printVersion={effectivePrintVersion} />
                  </> : <>
                    <section className="proofing-toolbar no-print" aria-label="逐页校对工具栏">
                      <div className="proofing-controls">
                        <div className="proofing-status">
                          <div className="proofing-status-title"><h2>{isRunning ? (detail.book.status === 'pausing' ? '正在暂停识别' : '正在逐页识别') : pendingPages.length ? `待识别 ${pendingPages.length} 页` : detail.pages.length ? '页面已全部识别' : '编排暂无页面'}</h2><span>{detail.book.completed_pages} / {detail.book.selected_page_count} 页</span></div>
                          <div className="progress-track" role="progressbar" aria-label="已编排页面识别进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: progress + '%' }} /></div>
                          <p>{isRunning ? '已开始的页面会完成并保存结果' : pendingPages.length ? '识别完成后，可逐页检查并保存校对' : detail.pages.length ? '可继续校对，或进入整书预览' : '返回页面编排，加入需要识别的页面'}</p>
                        </div>
                        <PaperSizeControl value={detail.book.paper_size ?? 'a4'} saving={layoutSaving} disabled={busy} onChange={savePaperSize} />
                        <div className="button-row proofing-actions">
                          <button onClick={() => changeView('organize')} disabled={processLocked}>修改编排</button>
                          {pagesToProcess.length > 0 && <button onClick={() => void processPages(pagesToProcess)} className="primary" disabled={processLocked}>{isRunning ? '处理中…' : '识别未完成页'}</button>}
                          {isRunning && <button onClick={pauseBook} disabled={actionBusy || detail.book.status === 'pausing'}>{detail.book.status === 'pausing' ? '正在暂停…' : '暂停处理'}</button>}
                          {!isRunning && detail.pages.length > 0 && pendingPages.length === 0 && <button className="primary" onClick={() => changeView('preview')} disabled={busy}>整书预览 <span aria-hidden="true">→</span></button>}
                        </div>
                      </div>
                      <ToolbarUsage usage={detail.book.usage} />
                    </section>
                    {detail.book.error && <div className="error-panel"><strong>处理失败</strong><span>{detail.book.error}</span></div>}
                    <div className="workspace-grid">
                      <aside className="page-rail" aria-label="已编排页面">{detail.pages.map((page, index) => <div className="page-row unconfirmed" key={page.number}><button className={page.number === selectedPageNumber ? 'page-link selected' : 'page-link'} onClick={() => choosePage(page)} disabled={busy} aria-current={page.number === selectedPageNumber ? 'page' : undefined}><span className="page-order-label">编排第 {index + 1} 页</span><span className="page-source-name" title={sourceLabel(page)}>{sourceLabel(page)}</span><span className={statusClass(page.status)}>{STATUS_LABEL[page.status] ?? page.status}</span></button></div>)}</aside>
                      {sourcePage ? <div className="proofing-area">
                        <div className="page-heading"><div><p className="eyebrow">编排第 {detail.pages.findIndex((page) => page.number === sourcePage.number) + 1} 页</p><div className="button-row"><h2 className="proofing-source-title">{sourceLabel(sourcePage)}</h2><span className={statusClass(sourcePage.status)}>{STATUS_LABEL[sourcePage.status] ?? sourcePage.status}</span><button onClick={() => void processPages([sourcePage.number])} disabled={processLocked} title="仅识别当前页面">{sourcePageHasResult ? '重新识别本页' : '识别本页'}</button></div></div><UsageView usage={sourcePage.usage} attempts={sourcePage.attempts} /></div>
                        {sourcePage.error && <div className="page-error">{sourcePage.error}</div>}
                        <div className="proofing-grid">
                          <section className="text-panel">
                            <div className="panel-title"><strong>本页校对</strong><button className="primary" onClick={savePage} disabled={!dirty || editLocked}>{saving ? '保存中…' : '保存本页'}</button></div>
                            {editLocked && <div className="lock-note">{sourcePage.status === 'processing' ? '本页正在识别，暂不可编辑；可切换到其他页面校对。' : '正在更新，本页暂不可编辑。'}</div>}
                            <PageEditor draft={pageDraft} onChange={(draft) => { setPageDraft(draft); setDirty(true) }} disabled={editLocked} />
                          </section>
                          <section className="render-panel">
                            <div className="panel-title"><strong>排版预览</strong><a href={api.pagePreviewUrl(detail.book.id, sourcePage.number)} target="_blank" rel="noreferrer">查看原页</a></div>
                            <div className="page-render">
                              <p className="page-layout-note">{pageDraft.page_kind === 'front_cover' ? '封面版式 · 自动应用' : pageDraft.page_kind === 'back_cover' ? '封底版式 · 自动应用' : '正文版式'}<span>{PAPER_SIZES[detail.book.paper_size ?? 'a4'].label}</span></p>
                              <PageContent page={{ ...sourcePage, ...pageDraft }} paperSize={detail.book.paper_size} />
                              {pageDraft.page_kind === 'content'
                                ? !pageDraft.text && <p className="empty-page">本页暂无正文</p>
                                : !pageDraft.cover_fields.some((field) => field.text.trim()) && <p className="empty-page">本页暂无书目信息</p>}
                            </div>
                          </section>
                        </div>
                      </div> : <div className="page-list-empty"><h2>编排暂无页面</h2><p>返回页面编排，选择需要识别和校对的页面。</p><button onClick={() => changeView('organize')} disabled={processLocked}>进入页面编排</button></div>}
                    </div>
                  </>}
              </>}
          </main>
        </div>
      )}
    </div>
  )
}
