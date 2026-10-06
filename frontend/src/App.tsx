import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type ImportError } from './api'
import { PdfPreview } from './PdfPreview'
import ProjectOrganizer from './ProjectOrganizer'
import WorkflowDashboard from './WorkflowDashboard'
import { PageEditor } from './PageEditor'
import { LayoutCalibration } from './LayoutCalibration'
import { SourceComparison } from './SourceComparison'
import type { ProofingFocus } from './SourceComparison'
import { calibrationFromPage } from './layoutDraft'
import { LayoutSettingsForm } from './LayoutSettingsForm'
import { ReasoningControl } from './ReasoningControl'
import type { ApiProtocol, BBox, Book, BookDetail, LayoutCalibrationUpdate, LayoutSettings, Notice, Page, PageDraft, PdfCompileResult, RenderDiagnostic, RenderStrategy, SelectionDraft, Settings } from './types'

const STATUS_LABEL: Record<string, string> = {
  uploaded: '待处理', processing: '处理中', pausing: '正在暂停', paused: '已暂停', ready: '已有结果',
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
  processing_concurrency: 2,
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误'
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
}

function geminiGenerationEndpoint(settings: Settings): string {
  const collection = endpoint(settings.base_url, settings.models_path).replace(/\/+$/, '')
  const model = settings.extraction_model.trim().replace(/^models\//, '')
  return `${collection}/${model ? encodeURIComponent(model) : '{model}'}:generateContent`
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
          processing_concurrency: value.processing_concurrency ?? 2,
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
        processing_concurrency: saved.processing_concurrency ?? 2,
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
      setTestResult(await api.testSettings())
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
        <label><span>自动识别与复核模型 ID</span><input value={settings.extraction_model} onChange={(event) => update('extraction_model', event.target.value)} placeholder="手动填写，或从上方列表选择" disabled={saving} /><small>{isGemini && '可填裸模型 ID 或 models/ 开头的完整资源名。'}任务使用所选模型识别、独立复核及必要的局部修复。</small></label>
        <ReasoningControl protocol={settings.api_protocol} value={settings.reasoning_effort} onChange={(value) => update('reasoning_effort', value)} disabled={saving} />
        <div className="field-grid compact-grid">
          <label><span>超时秒数</span><input type="number" min={5} max={600} value={settings.timeout_seconds} onChange={(event) => update('timeout_seconds', Number(event.target.value))} disabled={saving} /></label>
        </div>
        <label className="number-field">
          <span>处理并发数</span>
          <input type="number" min={1} step={1} value={processingConcurrency} onChange={(event) => { setProcessingConcurrency(event.target.value); setConcurrencyError(''); setTestResult(null) }} disabled={saving} aria-invalid={Boolean(concurrencyError)} aria-describedby={concurrencyError ? 'processing-concurrency-help processing-concurrency-error' : 'processing-concurrency-help'} />
          <small id="processing-concurrency-help">每个项目同时识别的最多页数，默认 2。请输入正整数，无固定上限。</small>
          {concurrencyError && <small id="processing-concurrency-error" className="field-error" role="alert">{concurrencyError}</small>}
        </label>
        {testResult && <div className={testResult.ok ? 'inline-result success-box' : 'inline-result error-box'}>配置检查：{testResult.message}</div>}
        <div className="button-row"><button className="primary" onClick={save} disabled={saving || modelsLoading || needsNewKey}>{saving ? '请稍候…' : '保存设置'}</button><button onClick={test} disabled={saving || modelsLoading || hasUnsavedChanges} title={hasUnsavedChanges ? '请先保存当前改动' : undefined}>检查已保存配置</button></div>
        {hasUnsavedChanges && <p className="settings-warning">有未保存改动。请先保存，再检查已保存配置。</p>}
        <p className="settings-footnote">获取模型只查询列表，不保存设置或发送推理请求。保存设置及配置检查不会调用模型；实际图片与结构化输出能力由首次正常页面请求确认。</p>
      </div>
    </main>
  )
}


type MainView = 'files' | 'select' | 'recognize' | 'advanced' | 'settings'
const UNFINISHED = new Set(['queued', 'running', 'pausing', 'paused', 'interrupted'])
function draftFromPage(page: Page): PageDraft {
  return { text: page.text, page_kind: page.page_kind, cover_fields: page.cover_fields, render_strategy: page.render_strategy, expected_content_revision: page.content_revision, expected_layout_revision: page.layout_revision }
}
function outputDraft(draft: PageDraft): PageDraft {
  return { ...draft, text: draft.page_kind === 'content' ? draft.text : '', cover_fields: draft.page_kind === 'content' ? [] : draft.cover_fields }
}

function AdvancedWorkspace({ bookId, active, onNotice, onUpdated, onDirtyChange }: {
  bookId: string; active: boolean; onNotice: (notice: Notice) => void; onUpdated: () => void; onDirtyChange: (dirty: boolean) => void
}) {
  const [detail, setDetail] = useState<BookDetail | null>(null), [error, setError] = useState('')
  const [number, setNumber] = useState<number | null>(null), [draft, setDraft] = useState<PageDraft | null>(null)
  const [calibration, setCalibration] = useState<LayoutCalibrationUpdate | null>(null)
  const [sourceDirty, setSourceDirty] = useState(false), [layoutDirty, setLayoutDirty] = useState(false), [bookDirty, setBookDirty] = useState(false)
  const [saving, setSaving] = useState(false), [compiling, setCompiling] = useState(false)
  const [preview, setPreview] = useState<PdfCompileResult | null>(null), [previewError, setPreviewError] = useState<string | null>(null)
  const [focus, setFocus] = useState<ProofingFocus | null>(null), [reload, setReload] = useState(0)
  const page = detail?.pages.find((item) => item.number === number) ?? null
  const dirty = sourceDirty || layoutDirty || bookDirty, locked = saving || compiling
  useEffect(() => {
    let current = true; setError('')
    api.getBook(bookId).then((value) => { if (current) { setDetail(value); setNumber((previous) => value.pages.some((item) => item.number === previous) ? previous : value.pages[0]?.number ?? null) } }).catch((cause) => { if (current) setError(errorText(cause)) })
    return () => { current = false }
  }, [bookId, reload])
  useEffect(() => {
    if (!page) { setDraft(null); setCalibration(null); return }
    setDraft(draftFromPage(page)); setCalibration(calibrationFromPage(page)); setSourceDirty(false); setLayoutDirty(false); setPreview(null); setPreviewError(null); setFocus(null)
  }, [page])
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false) }, [dirty, onDirtyChange])
  const adopt = (saved: Page) => { setDetail((current) => current ? { ...current, pages: current.pages.map((item) => item.number === saved.number ? saved : item) } : current); onUpdated() }
  const saveSource = async () => {
    if (!page || !draft) return
    setSaving(true)
    try { adopt(await api.savePage(bookId, page.number, outputDraft(draft))); onNotice({ kind: 'success', text: '人工稿已保存。' }) }
    catch (cause) { onNotice({ kind: 'error', text: errorText(cause) }) } finally { setSaving(false) }
  }
  const saveCalibration = async () => {
    if (!page || !calibration) return
    setSaving(true)
    try { adopt(await api.savePageLayout(bookId, page.number, calibration)); onNotice({ kind: 'success', text: '布局校准已保存。' }) }
    catch (cause) { onNotice({ kind: 'error', text: errorText(cause) }) } finally { setSaving(false) }
  }
  const compile = async (kind: 'source' | 'layout' | 'book') => {
    if (active) return
    setCompiling(true); setPreviewError(null); setFocus(null)
    try {
      const value = kind === 'book' ? await api.compileBook(bookId, false) : kind === 'layout' && page && calibration ? await api.compilePageLayout(bookId, page.number, calibration, false) : page && draft ? await api.compilePage(bookId, page.number, outputDraft(draft), false) : null
      setPreview(value)
    } catch (cause) { setPreviewError(errorText(cause)) } finally { setCompiling(false) }
  }
  const locate = (bbox: BBox | null, lineId?: string) => {
    if (page) setFocus({ token: Date.now(), book_id: bookId, page_number: page.number, content_revision: page.content_revision, layout_revision: page.layout_revision, source_bbox: bbox, output_bbox_bp: null, output_page: null, line_id: lineId ?? null })
  }
  const diagnostic = (value: RenderDiagnostic) => {
    if (page && value.page_number === page.number) setFocus({ token: Date.now(), book_id: bookId, page_number: page.number, content_revision: value.content_revision, layout_revision: value.layout_revision, source_bbox: value.source_bbox, output_bbox_bp: value.output_bbox_bp, output_page: value.output_page_start, line_id: value.line_id })
  }
  const saveBookLayout = async (layout: LayoutSettings, renderStrategy: RenderStrategy) => {
    setSaving(true)
    try { const book = await api.saveBookLayout(bookId, { layout, render_strategy: renderStrategy }); setDetail((current) => current ? { ...current, book } : current); setReload((value) => value + 1); onUpdated(); onNotice({ kind: 'success', text: '排版设置已保存；识别快照保留原设置。' }); return true }
    catch (cause) { onNotice({ kind: 'error', text: errorText(cause) }); return false } finally { setSaving(false) }
  }
  return <section className="advanced-workspace"><header className="section-heading"><p className="eyebrow">更多工具</p><h1>人工编辑与布局校准</h1><p>这里编辑当前人工稿。识别结果与下载继续使用各自的冻结快照。</p></header>
    {active && <p className="inline-result">后台识别正在运行。可以主动保存人工稿；本轮候选不会覆盖运行中新保存的修订。试编译在任务结束后可用。</p>}
    {error ? <div className="inline-result error-box">{error}<button onClick={() => setReload(reload + 1)}>重试读取</button></div> : !detail ? <p>正在按需读取当前稿…</p> : <>
      <details className="advanced-book-settings"><summary>整书排版设置</summary><LayoutSettingsForm layout={detail.book.layout} paperSize={detail.book.paper_size} renderStrategy={detail.book.render_strategy} dirty={bookDirty} saving={saving} disabled={locked || sourceDirty || layoutDirty} onDirtyChange={setBookDirty} onSave={saveBookLayout} /></details>
      <div className="advanced-page-choice"><label>当前稿页面<select value={number ?? ''} disabled={locked || dirty} onChange={(event) => setNumber(Number(event.target.value))}>{detail.pages.map((item) => <option key={item.page_id} value={item.number}>{item.source_filename} · 源第 {item.source_page} 页</option>)}</select></label><button disabled={locked || dirty} onClick={() => setReload(reload + 1)}>刷新当前稿</button>{dirty && <small>先保存或恢复当前改动，再切换页面。</small>}</div>
      {page && draft ? <>
        <SourceComparison key={page.page_id} bookId={bookId} page={page} result={preview} loading={compiling} focus={focus} />
        <details className="advanced-tool"><summary>布局校准</summary><LayoutCalibration key={page.page_id} page={page} draft={calibration} dirty={layoutDirty} disabled={locked || sourceDirty} saving={saving} onChange={(value) => { setCalibration(value); setLayoutDirty(true) }} onEditing={() => setLayoutDirty(true)} onSave={() => void saveCalibration()} onPreview={() => { if (active) onNotice({ kind: 'info', text: '后台识别结束后可试编译布局草稿。' }); else void compile('layout') }} onRestore={() => { setCalibration(calibrationFromPage(page)); setLayoutDirty(false) }} onLocate={locate} /></details>
        <details className="advanced-tool"><summary>源码与书目信息编辑</summary><p>保存人工稿受内容及布局修订保护。</p><PageEditor draft={draft} disabled={locked || layoutDirty} onChange={(value) => { setDraft({ ...value, render_strategy: value.text !== page.text && value.page_kind === 'content' ? 'custom_latex' : value.render_strategy }); setSourceDirty(true) }} /><div className="button-row"><button className="primary" disabled={locked || layoutDirty || !sourceDirty} onClick={() => void saveSource()}>{saving ? '保存中…' : '保存人工稿'}</button><button disabled={active || locked || layoutDirty} onClick={() => void compile('source')}>预览源码草稿</button><button disabled={locked || !sourceDirty} onClick={() => { setDraft(draftFromPage(page)); setSourceDirty(false) }}>恢复已保存稿</button></div></details>
      </> : <p className="center-state">当前选择没有可编辑页面，请先选页并识别。</p>}
      <details className="advanced-tool"><summary>当前稿 PDF 预览</summary><p>此预览来自当前稿，与识别结果快照分开显示。</p><button disabled={active || locked || dirty} onClick={() => void compile('book')}>生成当前稿预览</button><PdfPreview result={preview} loading={compiling} error={previewError} title="当前稿 / 草稿预览" emptyMessage="需要预览时再生成。" onDiagnostic={diagnostic} /></details>
    </>}
  </section>
}

export default function App() {
  const [books, setBooks] = useState<Book[]>([]), [detail, setDetail] = useState<BookDetail | null>(null), [selection, setSelection] = useState<SelectionDraft | null>(null)
  const [view, setView] = useState<MainView>('files'), [returnView, setReturnView] = useState<MainView>('files')
  const [runId, setRunId] = useState<string | null>(null), [active, setActive] = useState(false), [unfinished, setUnfinished] = useState(false)
  const [loading, setLoading] = useState(false), [uploading, setUploading] = useState(false), [creating, setCreating] = useState(false)
  const [title, setTitle] = useState(''), [notice, setNotice] = useState<Notice>(null), [importErrors, setImportErrors] = useState<ImportError[]>([])
  const [selectionDirty, setSelectionDirty] = useState(false), [advancedDirty, setAdvancedDirty] = useState(false), [advancedOpened, setAdvancedOpened] = useState(false), [settingsOpened, setSettingsOpened] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null), projectRequest = useRef(0), mutation = useRef(false)
  const refreshBooks = useCallback(() => { api.listBooks().then(setBooks).catch((cause) => setNotice({ kind: 'error', text: errorText(cause) })) }, [])
  const openProject = useCallback(async (bookId: string, requestedView?: MainView) => {
    const requestId = ++projectRequest.current; setLoading(true); setNotice(null)
    try {
      const [overview, draft, history] = await Promise.all([api.getOverview(bookId), api.getSelection(bookId), api.listRunSummaries(bookId)])
      if (requestId !== projectRequest.current) return
      const current = history.find((item) => UNFINISHED.has(item.status)) ?? history[0]
      setDetail(overview); setSelection(draft); setRunId(current?.run_id ?? null); setActive(Boolean(current && ['queued', 'running', 'pausing'].includes(current.status))); setUnfinished(Boolean(current && UNFINISHED.has(current.status))); setImportErrors([]); setSelectionDirty(false); setAdvancedDirty(false); setAdvancedOpened(false)
      setView(requestedView ?? (!overview.files.length ? 'files' : current || overview.book.selection_confirmed ? 'recognize' : 'select'))
      try { localStorage.setItem('ocr-active-project', bookId) } catch { /* Project selection also remains in memory. */ }
    } catch (cause) { if (requestId === projectRequest.current) setNotice({ kind: 'error', text: errorText(cause) }) }
    finally { if (requestId === projectRequest.current) setLoading(false) }
  }, [])
  useEffect(() => {
    let current = true
    api.listBooks().then((value) => { if (!current) return; setBooks(value); let remembered: string | null = null; try { remembered = localStorage.getItem('ocr-active-project') } catch { /* Use the list when browser storage is unavailable. */ } if (remembered && value.some((book) => book.id === remembered)) void openProject(remembered) }).catch((cause) => { if (current) setNotice({ kind: 'error', text: errorText(cause) }) })
    return () => { current = false; projectRequest.current += 1 }
  }, [openProject])
  const refreshOverview = useCallback(() => {
    if (!detail) return
    const bookId = detail.book.id
    api.getOverview(bookId).then((value) => { setDetail((current) => current?.book.id === bookId ? value : current); refreshBooks() }).catch((cause) => setNotice({ kind: 'error', text: errorText(cause) }))
  }, [detail?.book.id, refreshBooks])
  const create = async () => {
    if (mutation.current || !title.trim()) return
    mutation.current = true; setCreating(true)
    try { const book = await api.createProject(title.trim()); setTitle(''); refreshBooks(); await openProject(book.id, 'files') }
    catch (cause) { setNotice({ kind: 'error', text: errorText(cause) }) } finally { mutation.current = false; setCreating(false) }
  }
  const upload = async (files: File[]) => {
    if (!files.length || mutation.current) return
    mutation.current = true; setUploading(true); setNotice(null); setImportErrors([])
    try {
      let bookId = detail?.book.id
      if (!bookId) { const book = await api.createProject(title.trim() || files[0].name.replace(/\.[^.]+$/, '')); bookId = book.id; setTitle('') }
      const imported = await api.uploadFiles(bookId, files)
      setDetail({ book: imported.book, files: imported.files, pages: [] }); setSelection(null); setImportErrors(imported.import_errors ?? []); setView(imported.files.length ? 'select' : 'files')
      const [overview, draft, history] = await Promise.all([api.getOverview(bookId), api.getSelection(bookId), api.listRunSummaries(bookId)])
      setDetail(overview); setSelection(draft); setSelectionDirty(false); setAdvancedOpened(false)
      const current = history.find((item) => UNFINISHED.has(item.status))
      setRunId(current?.run_id ?? null); setActive(Boolean(current && ['queued', 'running', 'pausing'].includes(current.status))); setUnfinished(Boolean(current && UNFINISHED.has(current.status)))
      setImportErrors(imported.import_errors ?? []); setView(overview.files.length ? 'select' : 'files'); refreshBooks()
      try { localStorage.setItem('ocr-active-project', bookId) } catch { /* In-memory project remains usable. */ }
      if (imported.import_errors?.length) setNotice({ kind: 'info', text: '可导入的资料已保留；下方列出未导入文件及原因。' })
    } catch (cause) { setNotice({ kind: 'error', text: errorText(cause) }) } finally { mutation.current = false; setUploading(false); if (fileInput.current) fileInput.current.value = '' }
  }
  const savedSelection = (draft: SelectionDraft) => { setSelection(draft); setDetail((current) => current ? { ...current, book: { ...current.book, selection_confirmed: true, selected_page_count: draft.page_ids.length } } : current); setSelectionDirty(false); setView('recognize'); if (!unfinished) setRunId(null); else setNotice({ kind: 'info', text: '下一轮选择已保存，本轮继续使用启动时冻结的页面。' }); refreshOverview() }
  const settings = () => { if (view !== 'settings') setReturnView(view); setSettingsOpened(true); setView('settings') }
  const mainView = view === 'settings' ? returnView : view
  const pendingMutation = uploading || creating || loading
  return <div className="ocr-app">
    <header className="ocr-header"><button className="ocr-brand" onClick={() => setView('files')}>书页识别 <span>OCR V2</span></button><div><span>{detail?.book.title ?? '新项目'}</span><details className="ocr-more"><summary>更多</summary><button disabled={!detail || pendingMutation} onClick={() => { setAdvancedOpened(true); setView('advanced') }}>人工编辑与布局校准</button><button onClick={settings}>模型设置</button></details></div></header>
    <div className="ocr-shell"><aside className="ocr-projects"><h2>项目</h2><form onSubmit={(event) => { event.preventDefault(); void create() }}><input aria-label="新项目名称" placeholder="新项目名称" value={title} onChange={(event) => setTitle(event.target.value)} disabled={pendingMutation || advancedDirty} /><button disabled={pendingMutation || advancedDirty || !title.trim()}>{creating ? '创建中…' : '创建项目'}</button></form><ul>{books.map((book) => <li key={book.id}><button className={detail?.book.id === book.id ? 'selected' : ''} disabled={pendingMutation || advancedDirty} onClick={() => void openProject(book.id)}><strong>{book.title}</strong><span>{book.file_count} 份来源 · {book.page_count} 页</span><small>{STATUS_LABEL[book.status] ?? book.status}</small></button></li>)}</ul>{advancedDirty && <p>人工稿有未保存改动，请保存或恢复后切换项目。</p>}</aside>
    <main className="ocr-main">
      <nav className="ocr-steps" aria-label="主流程">{(['files', 'select', 'recognize'] as const).map((step, index) => <button key={step} className={mainView === step ? 'current' : ''} aria-current={mainView === step ? 'step' : undefined} disabled={pendingMutation || (step !== 'files' && !detail?.files.length) || (step === 'recognize' && !runId && (selectionDirty || !detail?.book.selection_confirmed))} onClick={() => setView(step)}><span>{index + 1}</span>{step === 'files' ? '文件' : step === 'select' ? '选页' : '识别'}</button>)}</nav>
      {view === 'settings' && <button className="ocr-back" onClick={() => setView(returnView)}>返回{ returnView === 'recognize' ? '识别' : returnView === 'select' ? '选页' : returnView === 'advanced' ? '更多工具' : '文件' }</button>}
      {notice && <div role={notice.kind === 'error' ? 'alert' : 'status'} className={notice.kind === 'error' ? 'inline-result error-box' : 'inline-result'}>{notice.text}<button aria-label="收起提示" onClick={() => setNotice(null)}>×</button></div>}
      {importErrors.length > 0 && <details className="ocr-import-errors" open><summary>{importErrors.length} 个文件未导入</summary><ul>{importErrors.map((item, index) => <li key={index}><strong>{item.filename}</strong>：{item.reason}</li>)}</ul></details>}
      {loading && <p className="center-state">正在读取项目…</p>}
      <section hidden={view !== 'files' || loading} className="ocr-files"><header className="section-heading"><p className="eyebrow">第一步</p><h1>文件</h1><p>添加 PDF 或图片，然后选择本轮需要识别的页面。</p></header><input ref={fileInput} className="ocr-file-input" type="file" multiple accept="application/pdf,image/*" disabled={pendingMutation || unfinished || advancedDirty || selectionDirty} onChange={(event) => void upload(Array.from(event.target.files ?? []))} />{selectionDirty && <p>选页有未保存改动，请先保存选择再添加资料。</p>}<div className="ocr-dropzone"><strong>{uploading ? '正在导入资料…' : detail?.files.length ? '继续添加资料' : '选择资料文件'}</strong><p>支持多个文件；源文件和历史识别结果保留。</p><button className="primary" disabled={pendingMutation || unfinished || advancedDirty || selectionDirty} onClick={() => fileInput.current?.click()}>{uploading ? '正在导入…' : '添加 PDF / 图片'}</button></div>{unfinished && <p>本项目还有未结束任务，继续处理后可添加资料。可进入选页为下一轮保存选择。</p>}{detail && <><ul className="ocr-file-list">{detail.files.map((file) => <li key={file.id}><strong>{file.filename}</strong><span>{file.kind.toUpperCase()} · {file.page_count} 页</span></li>)}</ul>{detail.files.length > 0 && <button disabled={pendingMutation} onClick={() => setView('select')}>进入选页 · {detail.book.page_count} 页</button>}</>}</section>
      {detail && !selection && view === 'select' && <div className="inline-result">尚未读取到选页草稿。<button onClick={() => api.getSelection(detail.book.id).then(setSelection).catch((cause) => setNotice({ kind: 'error', text: errorText(cause) }))}>重新读取选择</button></div>}
      {detail && selection && <div hidden={view !== 'select' || loading}><ProjectOrganizer key={detail.book.id + ':' + selection.selection_revision} bookId={detail.book.id} files={detail.files} selection={selection} total={detail.book.page_count} onSaved={savedSelection} onRefreshed={setSelection} onNotice={setNotice} onDirtyChange={setSelectionDirty} /></div>}
      {detail && selection && <div hidden={view !== 'recognize' || loading}><WorkflowDashboard key={detail.book.id} detail={detail} selection={selection} runId={runId} visible={view === 'recognize' && !loading} onRunIdChange={setRunId} onActiveChange={setActive} onUnfinishedChange={setUnfinished} onOrganize={() => setView('select')} onSettings={settings} onBookUpdated={refreshOverview} /></div>}
      {detail && advancedOpened && <div hidden={view !== 'advanced' || loading}><AdvancedWorkspace key={detail.book.id} bookId={detail.book.id} active={active} onNotice={setNotice} onUpdated={refreshOverview} onDirtyChange={setAdvancedDirty} /></div>}
      {settingsOpened && <div hidden={view !== 'settings'}><SettingsView onNotice={setNotice} /></div>}
    </main></div>
  </div>
}

