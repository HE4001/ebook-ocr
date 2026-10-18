import { useEffect, useRef, useState } from 'react'
import { ApiError, api, type OutputErrors } from './api'
import { PAPER_SIZES } from './paper'
import { CATEGORY_LABEL, FixedLayoutRules, scopeLabel } from './FixedLayoutRules'
import type { BookDetail, ContentBlock, OutputSnapshotPage, PageContent, PageOutcome, PageOutcomeSummary, PaperSize, RecognitionScope, Run, RunCreate, RunSummary, SelectionDraft, Settings, VisualLine } from './types'
import './workflow.css'

const ACTIVE = new Set(['queued', 'running', 'pausing'])
const LABEL: Record<string, string> = { prepare: '准备整页图像', recognize: '图像识别与内容分类', review: '独立复核内容', recover: '整页重读恢复', layout: '应用固定规则与准备图资源', render: '生成固定排版 PDF', export: '生成下载文件', queued: '等待处理', paused: '已暂停', interrupted: '处理已中断', pausing: '正在暂停', finished: '处理结束', failed: '处理结束', succeeded: '处理结束' }
const CONTENT = { usable: '可用', uncertain: '部分内容（有未辨认或遮挡片段）', unavailable: '未取得可用内容', unverified: '尚未完成复核' }
const LAYOUT = { faithful: '历史原版布局', approximate: '重排布局', unavailable: '尚未形成布局', unverified: '尚未完成判断' }
const FORMAT = { pending: '等待生成', generating: '正在生成', available: '可下载', failed: '生成失败' }
const PAGE_KIND: Record<PageContent['page_kind'], string> = { content: '正文', front_cover: '封面', back_cover: '封底', contents: '目录', blank: '空白', other: '其他' }
function stageLabel(scope: RecognitionScope | undefined, stage: string) {
  if (scope !== 'printed_fixed_layout') return ({ prepare: '准备历史源页', recognize: '识别历史内容', layout: '处理历史布局', render: '生成历史页面结果', recover: '恢复历史内容' } as Record<string, string>)[stage] ?? LABEL[stage] ?? stage
  return LABEL[stage] ?? stage
}
type Props = { detail: BookDetail; selection: SelectionDraft; runId: string | null; visible: boolean; onRunIdChange: (id: string | null) => void; onActiveChange: (active: boolean) => void; onUnfinishedChange: (unfinished: boolean) => void; onOrganize: () => void; onSettings: () => void; onBookUpdated: () => void }
const errorText = (error: unknown) => error instanceof Error ? error.message : '发生未知错误'
function summaryFromRun(run: Run): RunSummary {
  return { recognition_scope: run.recognition_scope, workflow_version: run.workflow_version, run_id: run.run_id, book_id: run.book_id, selection_revision: run.selection_revision, status: run.status, counts: run.counts, active_stages: {}, updated_at: run.updated_at, error: run.error, recent_errors: [], output_snapshot_id: run.output_snapshot_id, formats: {}, request_limit: run.request_limit, request_count: run.request_count, usage: run.usage, model_wait_started_at: null }
}
function outcomeLabel(outcome: PageOutcome | null) {
  if (!outcome) return '处理中'
  if (!outcome.source_readable) return '源页不可读取，未形成结果'
  if (outcome.source_disposition === 'page_preserved') return '未可靠文字化，已保留原页'
  if (outcome.source_disposition === 'regions_preserved') return '含保留区域'
  return outcome.content === 'usable' ? '可编辑识别结果' : CONTENT[outcome.content]
}
function contentLine(value: VisualLine) {
  return value.spans.map((span, index) => <span key={index} className={span.kind === 'text' ? `${span.bold ? 'content-bold' : ''} ${span.italic ? 'content-italic' : ''}` : ''}>{span.kind === 'math' ? <code>{span.text}</code> : span.text}</span>)
}
function BlockView({ block, scope }: { block: ContentBlock; scope: PageContent['recognition_scope'] }) {
  switch (block.type) {
    case 'latex': return <pre className="classified-latex" aria-label={`${CATEGORY_LABEL[block.category]} LaTeX 源码`}><code>{block.latex}</code></pre>
    case 'text': return <>{block.lines.map((item) => <div key={item.line_id} className={item.paragraph_start ? 'paragraph-start' : ''}>{contentLine(item)}</div>)}</>
    case 'equation': return <>{block.lines.map((item) => <div key={item.line_id} className="content-equation"><code>{item.latex}</code>{item.number && <span>{item.number}</span>}</div>)}</>
    case 'table': return <div className="content-table"><table><tbody>{Array.from({ length: block.rows }, (_, row) => <tr key={row}>{block.cells.filter((cell) => cell.row === row).sort((a, b) => a.column - b.column).map((cell) => <td key={cell.cell_id} rowSpan={cell.row_span} colSpan={cell.column_span}>{cell.preserved && scope === 'legacy_all_visible' ? `已保留源区域${cell.reason ? `：${cell.reason}` : ''}` : cell.lines.map((item) => <div key={item.line_id}>{contentLine(item)}</div>)}</td>)}</tr>)}</tbody></table></div>
    case 'figure': return <><p>图形说明：{block.description || '无文字说明'}{block.bbox && ' · 已记录源图位置'}</p>{scope === 'printed_fixed_layout' && <p>图形资源须经独立核验；不可用时输出保留占位和原因。资源是否形成请查看本页结果及输出说明。</p>}</>
  }
}
function ContentView({ content }: { content: PageContent }) {
  const fixed = content.recognition_scope === 'printed_fixed_layout'
  const represented = new Set(content.blocks.map((block) => block.source_region_id))
  const coverageNotices = content.recognition_scope === 'printed_original_only' ? [
    ...content.regions.filter((region) => region.layer === 'unknown' || (['printed', 'mixed'].includes(region.layer) && !represented.has(region.region_id))).map((region) => region.layer === 'unknown' ? `区域 ${region.region_id} 的印刷归属尚未确定` : `印刷区域 ${region.region_id} 尚未取得采用正文`),
    ...(!content.coverage_reviewed ? ['印刷覆盖核验尚未完成'] : []),
    ...(!content.regions.length && !content.blank ? ['印刷区域计划尚未建立；不能确认源页覆盖完整'] : []),
    ...(content.coverage_observations.some((item) => ['pending', 'unknown'].includes(item.scope_disposition)) ? ['仍有源区域尚未完成印刷范围归账'] : []),
  ] : []
  return <div className="recognized-content">
    <p>{scopeLabel(content.recognition_scope)}{fixed && ` · 规则版本 ${content.fixed_layout_version}`} · {PAGE_KIND[content.page_kind]}</p>
    {fixed && <p>以下按阅读顺序显示分类与 LaTeX 正文片段；完整模板及可用资源请下载本轮 LaTeX 包。每张源页可排成多个输出页，再按所选顺序合并。</p>}
    {fixed && !content.coverage_reviewed && <p className="content-uncertainty">完整源页的内容覆盖复核尚未完成。</p>}
    {coverageNotices.map((notice, index) => <p className="content-uncertainty" key={`coverage-${index}`}>未决印刷内容：{notice}</p>)}
    {content.unresolved_spans.map((span, index) => <p className="content-uncertainty" key={`unresolved-${index}`}>未决印刷片段：{span.reason}</p>)}
    {content.blank && <p>本页观察为空白页。</p>}
    {content.blocks.map((block, index) => <article key={block.block_id}>
      <div className="content-block-heading"><span className="content-category">{index + 1} · {block.type === 'latex' ? CATEGORY_LABEL[block.category] : block.type === 'figure' ? '图形' : `历史 ${block.type} 块`}</span><small>{CONTENT[block.review_status]}</small></div>
      <BlockView block={block} scope={content.recognition_scope} />
      {[...block.uncertainty, ...block.unresolved_reasons].length > 0 && <p className="content-uncertainty">{[...block.uncertainty, ...block.unresolved_reasons].join('；')}</p>}
    </article>)}
    {content.issues.filter((issue) => !issue.resolved).map((issue) => <p key={issue.issue_id} className="content-uncertainty">{issue.reason}</p>)}
  </div>
}

export default function WorkflowDashboard({ detail, selection, runId, visible, onRunIdChange, onActiveChange, onUnfinishedChange, onOrganize, onSettings, onBookUpdated }: Props) {
  const bookId = detail.book.id
  const [run, setRun] = useState<RunSummary | null>(null), [history, setHistory] = useState<RunSummary[]>([])
  const [settings, setSettings] = useState<Settings | null>(null), [settingsError, setSettingsError] = useState('')
  const [connectionError, setConnectionError] = useState(''), [watch, setWatch] = useState(0), [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false), [startError, setStartError] = useState('')
  const [limit, setLimit] = useState(''), [pageLimit, setPageLimit] = useState(12), [retryLimit, setRetryLimit] = useState(2), [recoveryLimit, setRecoveryLimit] = useState(2), [replace, setReplace] = useState(false)
  const [pagesOpen, setPagesOpen] = useState(false), [offset, setOffset] = useState(0), [pages, setPages] = useState<PageOutcomeSummary[]>([]), [pagesError, setPagesError] = useState('')
  const [selectedPage, setSelectedPage] = useState<PageOutcomeSummary | null>(null), [content, setContent] = useState<PageContent | null>(null), [contentError, setContentError] = useState(''), [reading, setReading] = useState(false)
  const [errorsOpen, setErrorsOpen] = useState(false), [outputErrors, setOutputErrors] = useState<OutputErrors | null>(null), [outputError, setOutputError] = useState('')
  const [now, setNow] = useState(Date.now())
  const [hasPending, setHasPending] = useState(false), [sourcePage, setSourcePage] = useState<PageOutcomeSummary | null>(null), [sourceError, setSourceError] = useState(false)
  const [snapshotPages, setSnapshotPages] = useState<Record<string, OutputSnapshotPage>>({})
  const starting = useRef(false), pending = useRef<RunCreate | null>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const pendingKey = `ocr-v2-start:${bookId}`
  const result = Boolean(run && !ACTIVE.has(run.status) && !['paused', 'interrupted'].includes(run.status))
  const unfinished = history.find((item) => ACTIVE.has(item.status) || ['paused', 'interrupted'].includes(item.status))

  useEffect(() => {
    pending.current = null
    try { const saved = sessionStorage.getItem(pendingKey); if (saved) pending.current = JSON.parse(saved) as RunCreate } catch { /* Retain the in-memory request when storage is unavailable. */ }
    setHasPending(Boolean(pending.current))
  }, [pendingKey])
  useEffect(() => {
    if (!run?.output_snapshot_id) return
    const controller = new AbortController()
    api.getOutputSnapshot(bookId, run.output_snapshot_id, controller.signal).then((snapshot) => { if (!controller.signal.aborted) setSnapshotPages(Object.fromEntries(snapshot.pages.map((page) => [page.page_id, page]))) }).catch(() => {})
    return () => controller.abort()
  }, [bookId, run?.output_snapshot_id, watch])

  useEffect(() => {
    const controller = new AbortController()
    api.listRunSummaries(bookId, controller.signal).then((value) => { if (!controller.signal.aborted) setHistory(value) }).catch(() => {})
    return () => controller.abort()
  }, [bookId, runId, run?.status])
  useEffect(() => {
    if (!visible || runId) return
    let current = true
    api.getSettings().then((value) => { if (current) { setSettings(value); setSettingsError('') } }).catch((error) => { if (current) setSettingsError(errorText(error)) })
    return () => { current = false }
  }, [visible, runId, bookId])
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined
    setPages([]); setSelectedPage(null); setSourcePage(null); setContent(null); setOffset(0); setOutputErrors(null); setSnapshotPages({})
    setRun((current) => current?.run_id === runId ? current : null)
    if (!runId) { setRun(null); setConnectionError(''); onActiveChange(false); return () => controller.abort() }
    setLoading(true)
    const poll = async () => {
      try {
        const value = await api.getRunSummary(bookId, runId, controller.signal)
        if (controller.signal.aborted) return
        setRun(value); setConnectionError(''); setLoading(false)
        if (ACTIVE.has(value.status)) timer = setTimeout(poll, 1600)
      } catch (error) {
        if (controller.signal.aborted) return
        setLoading(false); setConnectionError(errorText(error)); timer = setTimeout(poll, 4000)
      }
    }
    void poll()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [bookId, runId, watch, onActiveChange])
  useEffect(() => {
    if (run || !runId) { onActiveChange(Boolean(run && ACTIVE.has(run.status))); onUnfinishedChange(Boolean(run && (ACTIVE.has(run.status) || ['paused', 'interrupted'].includes(run.status)))) }
  }, [run, runId, onActiveChange, onUnfinishedChange])
  useEffect(() => { if (result) onBookUpdated() }, [run?.run_id, run?.status, onBookUpdated])
  useEffect(() => {
    if (!run?.model_wait_started_at || !ACTIVE.has(run.status)) return
    setNow(Date.now()); const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer)
  }, [run?.model_wait_started_at, run?.status])
  useEffect(() => {
    if (!visible || !runId || run?.workflow_version !== 2 || (!pagesOpen && !result)) return
    const controller = new AbortController()
    api.getRunOutcomes(bookId, runId, offset, 24, controller.signal).then((value) => { if (!controller.signal.aborted) { setPages(value); setPagesError('') } }).catch((error) => { if (!controller.signal.aborted) setPagesError(errorText(error)) })
    return () => controller.abort()
  }, [bookId, runId, visible, pagesOpen, result, offset, run?.updated_at, run?.status])
  useEffect(() => {
    if (!errorsOpen || !run?.output_snapshot_id) return
    const controller = new AbortController(); setOutputError('')
    api.getOutputErrors(bookId, run.output_snapshot_id, controller.signal).then((value) => { if (!controller.signal.aborted) setOutputErrors(value) }).catch((error) => { if (!controller.signal.aborted) setOutputError(errorText(error)) })
    return () => controller.abort()
  }, [errorsOpen, bookId, run?.output_snapshot_id, watch])
  useEffect(() => {
    if (!selectedPage || !runId) return
    const controller = new AbortController(); setContent(null); setContentError(''); setReading(true)
    api.getRunContent(bookId, runId, selectedPage.page_id, controller.signal).then((value) => { if (!controller.signal.aborted) setContent(value) }).catch((error) => { if (!controller.signal.aborted) setContentError(errorText(error)) }).finally(() => { if (!controller.signal.aborted) setReading(false) })
    return () => controller.abort()
  }, [bookId, runId, selectedPage])

  const start = async (continuation?: string) => {
    if (starting.current) return
    starting.current = true; setBusy(true); setStartError('')
    try {
      if (!pending.current) {
        try { const saved = sessionStorage.getItem(pendingKey); if (saved) pending.current = JSON.parse(saved) as RunCreate } catch { /* In-memory identity remains available if session storage is disabled. */ }
      }
      if (!pending.current) {
        if (!selection.valid || !selection.page_ids.length || !detail.book.selection_confirmed) throw new Error('请返回选页，保存当前有效选择。')
        if (!continuation && (!settings?.extraction_model || !settings.has_api_key)) throw new Error('请先保存模型和密钥设置。')
        const requestLimit = limit.trim() ? Number(limit) : undefined
        if (requestLimit !== undefined && (!Number.isInteger(requestLimit) || requestLimit < 0)) throw new Error('请求额度须为非负整数。')
        if (![pageLimit, retryLimit, recoveryLimit].every(Number.isInteger) || pageLimit < 1 || pageLimit > 100 || retryLimit < 0 || retryLimit > 10 || recoveryLimit < 0 || recoveryLimit > 10) throw new Error('请填写高级额度设置内允许的整数。')
        pending.current = { selection_revision: selection.selection_revision, client_request_id: crypto.randomUUID(), continuation_run_id: continuation, request_limit: requestLimit, policy: { replace_page_ids: replace ? selection.page_ids : [], requests_per_page: 6, page_request_limit: pageLimit, temporary_retry_limit: retryLimit, local_recovery_limit: recoveryLimit, compile_limit: 3 } }
        try { sessionStorage.setItem(pendingKey, JSON.stringify(pending.current)) } catch { /* Keep the same request in this mounted session. */ }
        setHasPending(true)
      }
      const created = await api.createRun(bookId, pending.current)
      pending.current = null; try { sessionStorage.removeItem(pendingKey) } catch { /* The confirmed run ID is already known. */ }
      if (!mounted.current) return
      setHasPending(false)
      setRun(summaryFromRun(created)); onRunIdChange(created.run_id); onBookUpdated()
    } catch (error) {
      if (error instanceof ApiError && [400, 404, 409, 422].includes(error.status ?? 0)) { pending.current = null; setHasPending(false); try { sessionStorage.removeItem(pendingKey) } catch { /* No accepted start is represented by this validation response. */ } }
      if (mounted.current) setStartError(`${errorText(error)}${pending.current ? ' 启动结果尚未确认，再次点击会确认同一个任务。' : ''}`)
    } finally { starting.current = false; if (mounted.current) setBusy(false) }
  }
  const control = async (resume: boolean) => {
    if (!run) return
    setBusy(true); setStartError('')
    try { const changed = resume ? await api.resumeRun(bookId, run.run_id) : await api.pauseRun(bookId, run.run_id); if (mounted.current) { setRun(summaryFromRun(changed)); setWatch((value) => value + 1) } }
    catch (error) { if (mounted.current) setStartError(errorText(error)) } finally { if (mounted.current) setBusy(false) }
  }
  const savedPaper = async (size: PaperSize) => {
    setBusy(true); setStartError('')
    try { await api.saveBookLayout(bookId, { paper_size: size }); if (mounted.current) onBookUpdated() } catch (error) { if (mounted.current) setStartError(errorText(error)) } finally { if (mounted.current) setBusy(false) }
  }
  const activeStages = Object.entries(run?.active_stages ?? {}).filter(([, count]) => count > 0)
  const stage = activeStages[0]?.[0]
  const completed = run?.counts.completed ?? 0, total = run?.counts.total ?? selection.page_ids.length
  const noText = Boolean(result && run?.workflow_version === 2 && !run.counts.editable && !run.counts.regions_preserved && !(run.counts.partial_content ?? 0))
  const allSource = Boolean(run && run.counts.page_preserved === total && total > 0)
  const snapshotId = run?.output_snapshot_id

  return <section className="workflow-v2">
    <header className="section-heading"><p className="eyebrow">第三步</p><h1>{run ? result ? '识别结果' : '识别' : '准备识别'}</h1></header>
    {(!run || run.recognition_scope === 'printed_fixed_layout') && <ol className="fixed-workflow-steps" aria-label="识别流程"><li>上传与选页</li><li>整页图像识别、内容分类</li><li>分类 LaTeX、固定规则</li><li>独立复核与文件生成</li></ol>}
    {run && <p className="workflow-scope">{scopeLabel(run.recognition_scope)}{run.recognition_scope !== 'printed_fixed_layout' && '。该任务沿用启动时的历史流程；重新开始识别才会使用固定排版。'}</p>}
    {run?.recognition_scope === 'printed_fixed_layout' && <details className="workflow-rules"><summary>查看本轮固定排版规则</summary><FixedLayoutRules /></details>}
    {connectionError && <div className="workflow-connection" role="status"><strong>连接中断，后台任务可能仍在运行</strong><p>{connectionError}</p><button onClick={() => setWatch(watch + 1)}>重新连接</button></div>}
    {hasPending && run && <div className="inline-result"><p>上次启动结果尚未确认。确认会复用原请求身份。</p><button disabled={busy} onClick={() => start()}>确认上次启动</button></div>}
    {run?.workflow_version === 2 && run.selection_revision !== selection.selection_revision && <p className="inline-result">本轮使用启动时冻结的页面；当前选择已保存为下一轮草稿。</p>}
    {startError && <div className="inline-result error-box" role="alert">{startError}</div>}
    {loading && !run && runId ? <p>正在恢复任务状态…</p> : !run ? <div className="workflow-ready"><h2>{detail.book.title}</h2><p>本轮将整页图像交给模型，识别印刷内容并分类输出 LaTeX，经独立复核后按固定规则重排并生成文件。</p><p>{detail.files.length} 份来源 · 已选择 {selection.page_ids.length} 页</p><FixedLayoutRules /><p>{settings ? `已保存模型：${settings.extraction_model || '未配置'} · ${settings.api_protocol === 'gemini' ? 'Gemini' : 'OpenAI Responses'}` : settingsError || '正在读取模型设置…'}</p>{(!settings?.extraction_model || !settings.has_api_key) && !hasPending && <button onClick={onSettings}>设置模型</button>}{hasPending && <p className="inline-result">上次启动结果尚未确认。继续确认会复用原请求身份。</p>}{unfinished && <p className="inline-result">还有未结束的{unfinished.workflow_version === 1 ? '旧版' : '当前'}任务，结束或继续该任务后才能开始新版任务。<button onClick={() => onRunIdChange(unfinished.run_id)}>返回未结束任务</button></p>}<div className="button-row"><button className="primary" disabled={busy || (!hasPending && (Boolean(unfinished) || !detail.book.selection_confirmed || !selection.valid || !selection.page_ids.length || !settings?.extraction_model || !settings.has_api_key))} onClick={() => start()}>{busy ? '正在确认启动…' : hasPending ? '继续确认上次启动' : '开始识别'}</button><button disabled={busy} onClick={onOrganize}>返回选页</button></div><details className="workflow-advanced"><summary>高级设置</summary><fieldset disabled={busy || hasPending}><p>新识别按固定排版规则生成；可调整输出纸型。自由字体、字号、边距及原页画布设置只供旧稿和人工编辑使用。</p><label>输出纸型<select value={detail.book.paper_size} disabled={busy} onChange={(event) => savedPaper(event.target.value as PaperSize)}>{Object.entries(PAPER_SIZES).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}</select></label><p>固定边距 25 mm、正文 11 pt；按内容重新折行与分页。</p><label>本轮请求额度<input value={limit} placeholder={`默认 ${selection.page_ids.length * 6}`} onChange={(event) => setLimit(event.target.value)} /></label><label>单页安全上限<input type="number" min={1} max={100} value={pageLimit} onChange={(event) => setPageLimit(Number(event.target.value))} /></label><label>每页暂时重试上限<input type="number" min={0} max={10} value={retryLimit} onChange={(event) => setRetryLimit(Number(event.target.value))} /></label><label>每页重读恢复轮数<input type="number" min={0} max={10} value={recoveryLimit} onChange={(event) => setRecoveryLimit(Number(event.target.value))} /></label><small>恢复轮数是安全上限；当前同一整页重读策略仅尝试一次。</small><label className="check-row"><input type="checkbox" checked={replace} onChange={(event) => setReplace(event.target.checked)} />允许本轮候选替换已有人工稿</label><small>默认保护已有稿；本轮新识别内容始终保留。运行中新保存的稿件仍受修订保护。</small><button onClick={onSettings}>模型并发与接入设置</button></fieldset></details></div> : run.workflow_version === 1 ? <div className="workflow-legacy"><h2>旧版任务记录</h2><p>此运行使用旧版处理流程，历史结果尚未经过 V2 内容与覆盖复核。</p><p>{LABEL[run.status] ?? run.status} · 已处理 {completed}/{total} 页</p>{run.error && <p>{run.error}</p>}<div className="button-row">{ACTIVE.has(run.status) && <button disabled={busy || run.status === 'pausing'} onClick={() => control(false)}>{run.status === 'pausing' ? '正在暂停旧版任务…' : '暂停旧版任务'}</button>}{['paused', 'interrupted'].includes(run.status) && <button className="primary" disabled={busy} onClick={() => control(true)}>沿旧版流程继续</button>}{result && <button onClick={() => onRunIdChange(null)}>使用当前选页建立新版任务</button>}</div>{!result && <p>这项旧版任务尚未结束，当前不能另建 V2 任务。新版选页仅保存为下一轮草稿。</p>}</div> : <>
      <div className={noText ? 'workflow-summary no-text' : 'workflow-summary'}><h2>{noText ? '本次未取得可用识别结果' : result ? '页面处理已结束' : run.status === 'paused' || run.status === 'interrupted' || run.status === 'pausing' ? LABEL[run.status] : stage ? `正在${stageLabel(run?.recognition_scope, stage)}` : completed === total && !result ? '页面处理结束，正在生成下载文件' : '正在自动处理'}</h2><p>已处理 {completed}/{total} 页</p><progress max={Math.max(1, total)} value={completed} aria-label="已结束处理页数" /><div className="workflow-result-counts"><span>可编辑识别结果 <b>{run.counts.editable}</b> 页</span><span>部分印刷内容 <b>{run.counts.partial_content ?? 0}</b> 页</span><span>含保留区域 <b>{run.counts.regions_preserved}</b> 页</span><span>仅原页 <b>{run.counts.page_preserved}</b> 页</span><span>未形成结果 <b>{run.counts.no_result}</b> 页</span></div>{run.counts.protected_existing > 0 && <p>另有 {run.counts.protected_existing} 页保护了当前旧稿；旧稿未计入本轮新识别成果。</p>}{activeStages.length > 1 && <p className="workflow-stages">{activeStages.map(([name, count]) => `${stageLabel(run.recognition_scope, name)} ${count} 页`).join(' · ')}</p>}{run.model_wait_started_at && ACTIVE.has(run.status) && <p role="status">模型处理中 · 已等待 {Math.max(0, Math.floor((now - Date.parse(run.model_wait_started_at)) / 1000))} 秒</p>}<div className="button-row">{['queued', 'running', 'pausing'].includes(run.status) && <button disabled={busy || run.status === 'pausing'} onClick={() => control(false)}>{run.status === 'pausing' ? '等待已发送请求结算…' : '暂停'}</button>}{['paused', 'interrupted'].includes(run.status) && <button className="primary" disabled={busy} onClick={() => control(true)}>继续处理</button>}{result && ((run.counts.partial_content ?? 0) > 0 || run.counts.no_result > 0 || run.counts.page_preserved > 0 || run.counts.regions_preserved > 0 || (['json', 'pdf', 'latex'] as const).some((name) => run.formats[name]?.status !== 'available')) && <button disabled={busy || hasPending} onClick={() => start(run.run_id)}>继续处理未完成项</button>}{result && <button onClick={() => onRunIdChange(null)}>按当前选页开始新识别</button>}{result && <button onClick={onOrganize}>返回选页</button>}</div>{run.error && <p className="workflow-error">{run.error}</p>}{run.recent_errors.some((error) => error.category === 'service_configuration') && <button onClick={onSettings}>修改接入设置</button>}</div>
      {snapshotId && <section className="workflow-outputs"><h2>同一快照的下载文件</h2><p>{run.recognition_scope === 'printed_fixed_layout' ? 'JSON 保存分类内容；LaTeX 包包含固定模板与引用图形，PDF 按固定规则重排。每种格式分别报告生成状态。' : '此快照来自历史流程，内容范围与排版策略以原任务为准。'}</p><div className="workflow-downloads">{(['pdf', 'json', 'latex'] as const).map((name) => { const format = run.formats[name]; return <div key={name}><strong>{name === 'pdf' ? 'PDF' : name === 'json' ? '内容 JSON' : 'LaTeX / 资源包'}</strong><span>{format ? FORMAT[format.status] : '等待生成'}</span>{format?.status === 'available' && <a href={api.snapshotDownloadUrl(bookId, snapshotId, name)} download>{name === 'pdf' && allSource ? '下载原页保留 PDF' : `下载 ${name === 'latex' ? 'LaTeX 资源包' : name.toUpperCase()}`}</a>}{format?.error && <p>{format.error}</p>}</div> })}</div>{run.formats.pdf?.status === 'available' && <details className="workflow-pdf" open={result}><summary>查看本快照 PDF</summary><iframe src={`${api.snapshotDownloadUrl(bookId, snapshotId, 'pdf')}?inline=true`} title="本轮冻结结果 PDF" loading="lazy" /></details>}<details onToggle={(event) => setErrorsOpen(event.currentTarget.open)}><summary>文件生成详情与缺失页</summary>{outputError && <p role="alert">{outputError}<button onClick={() => setWatch(watch + 1)}>重试读取</button></p>}{outputErrors ? (['pdf', 'latex'] as const).map((name) => <div key={name}><h3>{name.toUpperCase()}</h3>{!outputErrors[name].length ? <p>没有记录缺失页。</p> : <ol>{outputErrors[name].map((error, index) => <li key={`${error.page_id}-${index}`}>{error.source_filename} · 源第 {error.source_page} 页：{error.reason}</li>)}</ol>}</div>) : !outputError && <p>正在读取文件生成详情…</p>}</details></section>}
      <details open={result || pagesOpen} onToggle={(event) => { if (!result) setPagesOpen(event.currentTarget.open) }} className="workflow-pages"><summary>逐页状态与已识别内容</summary>{pagesError && <p role="alert">{pagesError}</p>}<ul>{pages.map((page) => <li key={page.page_id}><div><strong>所选第 {page.position + 1} 页{snapshotPages[page.page_id] && ` · ${snapshotPages[page.page_id].source_filename} · 源第 ${snapshotPages[page.page_id].source_page} 页`}</strong><span>{outcomeLabel(page.outcome)}{!page.outcome && ` · ${stageLabel(run.recognition_scope, page.stage)}`}</span>{page.outcome && <small>内容：{CONTENT[page.outcome.content]} · 排版：{run.recognition_scope === 'printed_fixed_layout' && page.outcome.layout === 'approximate' ? '固定规则重排' : LAYOUT[page.outcome.layout]}</small>}{page.outcome?.errors.map((error, index) => <p key={index} className="workflow-error">{error.message}</p>)}</div><div className="workflow-page-actions"><button onClick={() => { setSourcePage(page); setSourceError(false) }}>查看原页</button><button onClick={() => setSelectedPage(page)} disabled={!page.outcome?.content_revision_id}>{run.recognition_scope === 'printed_fixed_layout' ? '分类与 LaTeX' : '历史内容'}</button></div></li>)}</ul><div className="selection-pagination"><button disabled={!offset} onClick={() => setOffset(offset - 24)}>上一组</button><span>{total ? offset + 1 : 0}—{Math.min(offset + 24, total)} / {total}</span><button disabled={offset + 24 >= total} onClick={() => setOffset(offset + 24)}>下一组</button></div></details>
      {sourcePage && <section className="workflow-source"><header><h2>所选第 {sourcePage.position + 1} 页 · 本轮冻结原页</h2><button onClick={() => setSourcePage(null)}>收起</button></header>{sourceError ? <p role="alert">原页预览未载入。<button onClick={() => setSourceError(false)}>重试</button></p> : <img src={api.sourcePreviewUrl(bookId, sourcePage.page_id, run.run_id, sourcePage.outcome?.source_version ?? snapshotPages[sourcePage.page_id]?.source_version)} alt="本次识别使用的源页" onError={() => setSourceError(true)} loading="lazy" />}</section>}
      {selectedPage && <section className="workflow-content"><header><h2>所选第 {selectedPage.position + 1} 页 · {run.recognition_scope === 'printed_fixed_layout' ? '分类与 LaTeX' : '历史内容'}</h2><div className="button-row"><button onClick={() => { setSourcePage(selectedPage); setSourceError(false) }}>对照原图</button><button onClick={() => { setSelectedPage(null); setContent(null) }}>收起</button></div></header>{reading ? <p>正在读取内容…</p> : contentError ? <p role="alert">{contentError}</p> : content ? <ContentView content={content} /> : <p>本页尚无已保存的识别内容。</p>}</section>}
      <details className="workflow-technical"><summary>处理详情</summary><dl><dt>运行身份</dt><dd>{run.run_id}</dd><dt>选择修订</dt><dd>{run.selection_revision}</dd><dt>请求占用 / 上限</dt><dd>{run.request_count} / {run.request_limit}</dd><dt>已知 token 用量</dt><dd>{run.usage.total_tokens ?? '未知'}{!run.usage.complete && ' · 可能不完整'}</dd><dt>输出快照</dt><dd>{snapshotId ?? '尚未形成'}</dd></dl>{run.recent_errors.map((error, index) => <p key={index}>{error.phase} / {error.stage} / {error.category} {error.field_path}: {error.message}</p>)}</details>
    </>}
    {history.length > 0 && <details className="workflow-history"><summary>历史任务</summary><ul>{history.map((item) => <li key={item.run_id}><button disabled={busy || ACTIVE.has(run?.status ?? '')} onClick={() => onRunIdChange(item.run_id)}>{item.workflow_version === 1 ? '历史 V1 · 未经新版复核' : scopeLabel(item.recognition_scope)} · {LABEL[item.status] ?? item.status} · {item.counts.total} 页 · {new Date(item.updated_at).toLocaleString()}</button></li>)}</ul></details>}
  </section>
}
