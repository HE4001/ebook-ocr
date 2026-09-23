import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { BookContent, Markdown } from './Markdown'
import { buildStandaloneHtml, downloadText, safeFilename } from './exportHtml'
import type { Book, BookDetail, Notice, Page, Settings, Usage } from './types'

const STATUS_LABEL: Record<string, string> = {
  uploaded: '待处理', processing: '处理中', pausing: '正在暂停', paused: '已暂停', ready: '已完成',
  failed: '失败', interrupted: '已中断',
}

const EMPTY_SETTINGS: Settings = {
  base_url: 'https://api.openai.com/v1',
  responses_path: '/responses',
  extraction_model: '',
  reasoning_effort: '',
  classification_model: '',
  api_key: '',
  has_api_key: false,
  structured_output: false,
  max_output_tokens: 12000,
  timeout_seconds: 120,
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
  return current.base_url === saved.base_url
    && current.responses_path === saved.responses_path
    && current.extraction_model === saved.extraction_model
    && current.reasoning_effort === saved.reasoning_effort
    && current.max_output_tokens === saved.max_output_tokens
    && current.timeout_seconds === saved.timeout_seconds
}

function connectionTestMessage(result: { ok: boolean; message: string }) {
  if (!result.ok && /模型服务\s+HTTP\s+404/i.test(result.message)) {
    return { ...result, message: '模型服务返回 HTTP 404：请检查实际 POST 地址、Responses 接入路径和模型名称。这不能证明 API 密钥有误。' }
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

function SettingsView({ onNotice }: { onNotice: (notice: Notice) => void }) {
  const [settings, setSettings] = useState<Settings>(EMPTY_SETTINGS)
  const [savedSettings, setSavedSettings] = useState<Settings | null>(null)
  const [clearKey, setClearKey] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    api.getSettings()
      .then((value) => {
        const loaded = { ...value, reasoning_effort: value.reasoning_effort ?? '', max_output_tokens: value.max_output_tokens ?? 12000, api_key: '' }
        setSettings(loaded)
        setSavedSettings(loaded)
      })
      .catch((error) => onNotice({ kind: 'error', text: errorText(error) }))
      .finally(() => setLoading(false))
  }, [onNotice])

  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setSettings((current) => ({ ...current, [key]: value }))
    setTestResult(null)
  }

  const hasUnsavedChanges = savedSettings !== null && (
    !sameSavedSettings(settings, savedSettings)
    || Boolean(settings.api_key?.trim())
    || clearKey
  )
  const validMaxOutputTokens = Number.isInteger(settings.max_output_tokens) && settings.max_output_tokens > 0

  const fillDeepSeek = () => {
    setSettings((current) => ({
      ...current,
      base_url: 'https://api.deepseek.com',
      responses_path: '/responses',
      extraction_model: 'deepseek-flash',
      reasoning_effort: 'high',
      structured_output: false,
      max_output_tokens: 12000,
    }))
    setTestResult(null)
  }

  const save = async () => {
    setSaving(true)
    setTestResult(null)
    try {
      const saved = await api.saveSettings(settings, clearKey)
      const loaded = { ...saved, reasoning_effort: saved.reasoning_effort ?? '', max_output_tokens: saved.max_output_tokens ?? 12000, api_key: '' }
      setSettings(loaded)
      setSavedSettings(loaded)
      setClearKey(false)
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
      setTestResult(connectionTestMessage(await api.testSettings()))
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
        <div className="provider-preset"><button type="button" onClick={fillDeepSeek} disabled={saving}>填入 DeepSeek 配置</button><small>替换接入地址、模型、推理程度和最大输出 token；不会更改密钥或超时。仅填入表单，保存后生效。若当前密钥不是 DeepSeek 密钥，请自行替换。</small><small>图片识别使用 deepseek-flash；推理程度可填 none、low、high、max，留空使用模型默认值。<a href="https://api-docs.deepseek.com/zh-cn/guides/responses_api/" target="_blank" rel="noreferrer">Responses 指南</a> · <a href="https://api-docs.deepseek.com/zh-cn/api/create-response/" target="_blank" rel="noreferrer">参数文档</a></small></div>
        <label><span>API 根地址</span><input value={settings.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder="https://api.openai.com/v1" /><small>包含协议和 API 版本；接入路径留空时，此地址就是完整请求地址。</small></label>
        <label><span>Responses 接入路径</span><input value={settings.responses_path} onChange={(event) => update('responses_path', event.target.value)} placeholder="/responses" /><small>可留空，或填写自定义相对路径。</small></label>
        <div className="endpoint-preview"><span>实际 POST 地址</span><code>{endpoint(settings.base_url, settings.responses_path)}</code>{settings.responses_path.trim() === '' && <small>接入路径为空，API 根地址会直接作为完整请求地址。</small>}{settings.responses_path.trim() === '/' && <small>当前路径为 /，请求会发到 API 根地址。</small>}</div>
        <label><span>页面代理模型</span><input value={settings.extraction_model} onChange={(event) => update('extraction_model', event.target.value)} /><small>每页直接返回排版好的 Markdown，不要求 JSON。</small></label>
        <label><span>模型推理程度</span><input value={settings.reasoning_effort} onChange={(event) => update('reasoning_effort', event.target.value)} placeholder="例如 low、medium、high" /><small>手动填写模型供应商支持的值；留空则使用服务默认值。</small></label>
        <label className="number-field"><span>最大输出 token</span><input type="number" min={1} step={1} value={settings.max_output_tokens} onChange={(event) => update('max_output_tokens', Number(event.target.value))} /><small>包含推理和正文；识别与连接测试均使用。提高额度可能增加用量。</small></label>
        <label><span className="field-title">API 密钥 <span className={settings.has_api_key ? 'key-state saved' : 'key-state missing'}>{settings.has_api_key ? '已保存到本机' : '未配置'}</span></span><input type="password" autoComplete="new-password" value={settings.api_key ?? ''} onChange={(event) => update('api_key', event.target.value)} placeholder={settings.has_api_key ? '已保存；留空保留' : '输入密钥'} disabled={clearKey} /><small>已保存密钥不会回显；留空保留，输入新密钥替换。密钥以明文保存在本机 SQLite 数据库中，重启后自动读取，不保存在浏览器中。</small></label>
        <label className="check-row"><input type="checkbox" checked={clearKey} onChange={(event) => { const checked = event.target.checked; setClearKey(checked); setTestResult(null); if (checked) update('api_key', '') }} /><span>清除本机已保存的密钥</span></label>
        <div className="field-grid compact-grid">
          <label><span>超时秒数</span><input type="number" min={5} max={600} value={settings.timeout_seconds} onChange={(event) => update('timeout_seconds', Number(event.target.value))} /></label>
        </div>
        {testResult && <div className={testResult.ok ? 'inline-result success-box' : 'inline-result error-box'}>{testResult.message}</div>}
        <div className="button-row"><button className="primary" onClick={save} disabled={saving || !validMaxOutputTokens}>{saving ? '请稍候…' : '保存设置'}</button><button onClick={test} disabled={saving || hasUnsavedChanges} title={hasUnsavedChanges ? '请先保存当前改动' : undefined}>主动测试连接</button></div>
        {!validMaxOutputTokens && <p className="settings-warning">最大输出 token 请填写正整数。</p>}
        {hasUnsavedChanges && <p className="settings-warning">有未保存改动。请先保存，再测试模型连接。</p>}
        <p className="settings-footnote">保存设置不会联系模型服务。主动测试使用已保存配置发送请求，可能产生供应商用量。</p>
      </div>
    </main>
  )
}

export default function App() {
  const [view, setView] = useState<'workspace' | 'preview' | 'settings'>('workspace')
  const [books, setBooks] = useState<Book[]>([])
  const [selectedBookId, setSelectedBookId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BookDetail | null>(null)
  const [selectedPageNumber, setSelectedPageNumber] = useState(1)
  const [draftText, setDraftText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [initialLoading, setInitialLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const selectedBookIdRef = useRef<string | null>(null)
  const showNotice = useCallback((value: Notice) => setNotice(value), [])

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

  const loadDetail = useCallback(async (id: string, quiet = false) => {
    if (!quiet) setDetailLoading(true)
    try {
      const value = await api.getBook(id)
      if (id !== selectedBookIdRef.current) return
      setDetail(value)
      setBooks((current) => current.map((book) => book.id === value.book.id ? value.book : book))
      setSelectedPageNumber((current) => value.pages.some((page) => page.number === current) ? current : value.pages[0]?.number ?? 1)
    } catch (error) {
      if (!quiet && id === selectedBookIdRef.current) setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      if (!quiet && id === selectedBookIdRef.current) setDetailLoading(false)
    }
  }, [])

  useEffect(() => { void loadBooks() }, [loadBooks])
  useEffect(() => {
    if (!selectedBookId) { setDetail(null); return }
    setDirty(false)
    setDetail(null)
    void loadDetail(selectedBookId)
  }, [selectedBookId, loadDetail])
  useEffect(() => {
    if (!selectedBookId || !['processing', 'pausing'].includes(detail?.book.status ?? '')) return
    const timer = window.setInterval(() => void loadDetail(selectedBookId, true), 2000)
    return () => window.clearInterval(timer)
  }, [selectedBookId, detail?.book.status, loadDetail])

  const sourcePage = useMemo(() => detail?.pages.find((page) => page.number === selectedPageNumber) ?? null, [detail, selectedPageNumber])
  useEffect(() => { if (!dirty) setDraftText(sourcePage?.text ?? '') }, [sourcePage, dirty])

  const choosePage = (page: Page) => {
    if (page.number === selectedPageNumber) return
    if (dirty && !window.confirm('本页有未保存修改，确定切换并放弃吗？')) return
    setSelectedPageNumber(page.number)
    setDraftText(page.text)
    setDirty(false)
  }

  const chooseBook = (id: string) => {
    if (deleting) return
    if (id === selectedBookId) return
    if (dirty && !window.confirm('当前页有未保存修改，确定切换书籍并放弃吗？')) return
    setDirty(false)
    selectedBookIdRef.current = id
    setSelectedBookId(id)
  }

  const upload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    if (deleting) return
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setUploading(true)
    setNotice({ kind: 'info', text: '正在导入 ' + file.name + '…' })
    try {
      const book = await api.uploadBook(file)
      setBooks((current) => [book, ...current.filter((item) => item.id !== book.id)])
      selectedBookIdRef.current = book.id
      setSelectedBookId(book.id)
      setNotice({ kind: 'success', text: '已导入 ' + book.title + '，可开始处理。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setUploading(false)
    }
  }

  const processBook = async () => {
    if (!selectedBookId) return
    setActionBusy(true)
    try {
      const result = await api.processBook(selectedBookId)
      setNotice({ kind: 'success', text: result.started ? '处理已开始，将逐页更新状态。' : '当前没有需要处理的页面。' })
      await loadDetail(selectedBookId, true)
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
      setNotice({ kind: 'info', text: '已请求暂停。当前页完成后会停止处理后续页面。' })
      await loadDetail(selectedBookId, true)
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
      await loadDetail(selectedBookId, true)
    } finally {
      setActionBusy(false)
    }
  }

  const deleteBook = async () => {
    if (!detail || deleting || uploading || actionBusy || saving || ['processing', 'pausing'].includes(detail.book.status)) return
    const book = detail.book
    if (!window.confirm(`确定删除《${book.title}》吗？这会删除该书的源文件、识别和校对结果，且无法撤销。`)) return
    setDeleting(true)
    try {
      await api.deleteBook(book.id)
      const remaining = books.filter((item) => item.id !== book.id)
      setBooks(remaining)
      selectedBookIdRef.current = remaining[0]?.id ?? null
      setSelectedBookId(selectedBookIdRef.current)
      setDetail(null)
      setSelectedPageNumber(1)
      setDraftText('')
      setDirty(false)
      setView('workspace')
      setNotice({ kind: 'success', text: `已删除《${book.title}》。` })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setDeleting(false)
    }
  }

  const savePage = async () => {
    if (!detail || !sourcePage) return
    setSaving(true)
    try {
      const saved = await api.savePage(detail.book.id, sourcePage.number, draftText)
      setDetail((current) => current ? { ...current, pages: current.pages.map((page) => page.number === saved.number ? saved : page) } : current)
      setDraftText(saved.text)
      setDirty(false)
      setNotice({ kind: 'success', text: '第 ' + saved.number + ' 页已保存。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setSaving(false)
    }
  }

  const exportData = async (kind: 'json' | 'html' | 'md') => {
    if (!detail) return
    setActionBusy(true)
    try {
      if (kind === 'md') {
        downloadText(safeFilename(detail.book.title) + '.md', await api.exportMarkdown(detail.book.id), 'text/markdown;charset=utf-8')
        setNotice({ kind: 'success', text: 'Markdown 已下载。' })
        return
      }
      const data = await api.exportBook(detail.book.id)
      const name = safeFilename(data.book.title)
      if (kind === 'json') downloadText(name + '.json', JSON.stringify(data, null, 2), 'application/json;charset=utf-8')
      if (kind === 'html') downloadText(name + '.html', buildStandaloneHtml(data), 'text/html;charset=utf-8')
      setNotice({ kind: 'success', text: (kind === 'html' ? '独立 HTML' : 'JSON') + ' 已下载。' })
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) })
    } finally {
      setActionBusy(false)
    }
  }

  const printBook = () => { setView('preview'); window.setTimeout(() => window.print(), 80) }
  const isRunning = detail?.book.status === 'processing' || detail?.book.status === 'pausing'
  const editLocked = isRunning || deleting || sourcePage?.status === 'processing'
  const progress = detail?.book.page_count ? Math.round(detail.book.completed_pages / detail.book.page_count * 100) : 0
  const oldBackendContract = Boolean(detail && (
    !detail.book.usage || detail.pages.some((page) => typeof page.text !== 'string' || !page.usage)
  ))

  return (
    <div className="app-shell">
      <header className="topbar no-print">
        <button className="brand" onClick={() => setView('workspace')}><span className="brand-mark">页</span><span>纸页重排</span></button>
        <nav aria-label="主导航">
          <button className={view === 'workspace' ? 'nav-active' : ''} onClick={() => setView('workspace')}>逐页校对</button>
          <button className={view === 'preview' ? 'nav-active' : ''} onClick={() => setView('preview')} disabled={!detail || deleting}>整书预览</button>
          <button className={view === 'settings' ? 'nav-active' : ''} onClick={() => setView('settings')}>设置</button>
        </nav>
      </header>
      {notice && <div className={'notice ' + notice.kind + ' no-print'} role="status"><span>{notice.text}</span><button onClick={() => setNotice(null)} aria-label="关闭提示">×</button></div>}
      {view === 'settings' ? <SettingsView onNotice={showNotice} /> : (
        <div className="layout">
          <aside className="library no-print">
            <div className="library-heading"><div><p className="eyebrow">本地项目</p><h2>书库</h2></div><button className="compact-button" onClick={() => fileInput.current?.click()} disabled={uploading || deleting}>{uploading ? '导入中…' : '＋ 导入'}</button></div>
            <input ref={fileInput} className="visually-hidden" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" onChange={upload} />
            {initialLoading ? <p className="sidebar-state">正在读取书库…</p> : books.length === 0 ? <div className="library-empty"><p>还没有书籍</p><span>导入 PDF、PNG 或 JPEG 开始。</span></div> : (
              <div className="book-list">{books.map((book) => (
                <button className={book.id === selectedBookId ? 'book-item selected' : 'book-item'} onClick={() => chooseBook(book.id)} disabled={deleting} key={book.id}>
                  <span className="book-item-title">{book.title}</span>
                  <span className="book-item-meta"><span className={statusClass(book.status)}>{STATUS_LABEL[book.status] ?? book.status}</span><span>{book.page_count} 页</span></span>
                </button>
              ))}</div>
            )}
          </aside>
          <main className="main-panel">
            {!selectedBookId ? <div className="welcome-state"><span className="welcome-icon">文</span><h1>从纸页到可读书稿</h1><p>导入扫描 PDF 或图片，逐页提取并校对 Markdown 文本。</p><button className="primary" onClick={() => fileInput.current?.click()}>导入第一本书</button></div>
              : detailLoading || !detail || detail.book.id !== selectedBookId ? <div className="center-state">正在读取书籍…</div>
              : oldBackendContract ? <div className="center-state"><h2>后端仍在运行旧版本</h2><p>请使用 stop.bat 和 start.bat 重启后端，以加载逐页代理与文本接口。</p></div>
              : view === 'preview' ? (
                <>
                  <div className="preview-toolbar no-print">
                    <div><p className="eyebrow">整书预览</p><h1>{detail.book.title}</h1><p>按源页顺序显示文本，保留页眉、页脚、页码及 Markdown 格式。</p><UsageView usage={detail.book.usage} /></div>
                    <div className="button-row"><button onClick={() => void exportData('json')} disabled={actionBusy}>下载 JSON</button><button onClick={() => void exportData('md')} disabled={actionBusy}>下载 Markdown</button><button onClick={() => void exportData('html')} disabled={actionBusy}>下载独立 HTML</button><button className="primary" onClick={printBook} disabled={actionBusy}>打印 / 存为 PDF</button></div>
                    <p className="export-limit">独立 HTML 不加载远程资源，公式以 MathML 保存，离线显示取决于浏览器数学字体。</p>
                  </div>
                  <BookContent detail={detail} />
                </>
              ) : (
                <>
                  <header className="book-header">
                    <div className="book-heading"><p className="eyebrow">{detail.book.filename}</p><h1>{detail.book.title}</h1><div className="book-summary"><span className={statusClass(detail.book.status)}>{STATUS_LABEL[detail.book.status] ?? detail.book.status}</span><span>{detail.book.completed_pages} / {detail.book.page_count} 页</span></div><UsageView usage={detail.book.usage} /></div>
                    <div className="book-actions"><button onClick={processBook} className="primary" disabled={actionBusy || isRunning || deleting}>{isRunning ? '处理中…' : detail.book.status === 'ready' ? '处理未完成页' : detail.book.status === 'paused' ? '继续处理' : '开始 / 继续处理'}</button>{isRunning && <button onClick={pauseBook} disabled={actionBusy || detail.book.status === 'pausing'}>{detail.book.status === 'pausing' ? '正在暂停…' : '暂停处理'}</button>}<button onClick={() => setView('preview')} disabled={deleting}>整书预览</button><button className="danger-action" onClick={deleteBook} disabled={isRunning || actionBusy || saving || uploading || deleting}>{deleting ? '删除中…' : '删除此书'}</button></div>
                  </header>
                  <div className="progress-track" aria-label={'处理进度 ' + progress + '%'}><span style={{ width: progress + '%' }} /></div>
                  {detail.book.error && <div className="error-panel"><strong>处理失败</strong><span>{detail.book.error}</span></div>}
                  <div className="workspace-grid">
                    <aside className="page-rail" aria-label="页面列表">{detail.pages.map((page) => <button key={page.number} className={page.number === selectedPageNumber ? 'page-link selected' : 'page-link'} onClick={() => choosePage(page)} disabled={deleting}><span>第 {page.number} 页</span><span className={statusClass(page.status)}>{STATUS_LABEL[page.status] ?? page.status}</span></button>)}</aside>
                    {sourcePage ? (
                      <div className="proofing-area">
                        <div className="page-heading"><div><p className="eyebrow">逐页结果</p><h2>第 {sourcePage.number} 页</h2><span className={statusClass(sourcePage.status)}>{STATUS_LABEL[sourcePage.status] ?? sourcePage.status}</span></div><UsageView usage={sourcePage.usage} attempts={sourcePage.attempts} /></div>
                        {sourcePage.error && <div className="page-error">{sourcePage.error}</div>}
                        <div className="proofing-grid">
                          <section className="text-panel"><div className="panel-title"><strong>Markdown 源文本</strong><button className="primary" onClick={savePage} disabled={!dirty || saving || editLocked}>{saving ? '保存中…' : '保存本页'}</button></div>{editLocked && <div className="lock-note">{deleting ? '删除中，本页暂不可编辑。' : '处理运行中，本页暂不可编辑。'}</div>}<textarea aria-label="本页 Markdown 源文本" value={draftText} onChange={(event) => { setDraftText(event.target.value); setDirty(true) }} disabled={editLocked} spellCheck={false} placeholder="本页暂无文本" /></section>
                          <section className="render-panel"><div className="panel-title"><strong>预览</strong><span>按 Markdown 显示</span></div><div className="page-render">{draftText ? <Markdown text={draftText} /> : <p className="empty-page">本页暂无文本</p>}</div></section>
                        </div>
                      </div>
                    ) : <div className="center-state">暂无页面。</div>}
                  </div>
                </>
              )}
          </main>
        </div>
      )}
    </div>
  )
}
