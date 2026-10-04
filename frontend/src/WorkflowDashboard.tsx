import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { downloadBlob, safeFilename } from './downloads'
import { PAPER_SIZES } from './paper'
import { resolvePageSelection } from './pageSelection'
import type { BBox, BookDetail, CheckStatus, ExportManifest, Issue, PageResult, PaperSize, Run, RunCreate, Settings, WorkflowStage } from './types'
import './workflow.css'

const PAGE_SIZE = 100
const ACTIVE = new Set(['queued', 'running', 'pausing'])
const RUN_LABEL: Record<Run['status'], string> = { queued: '排队中', running: '自动处理中', pausing: '正在暂停', paused: '已暂停', succeeded: '处理结束', failed: '处理失败', interrupted: '已中断' }
const RESULT_LABEL = { auto_passed: '自动通过', completed_with_issues: '带问题完成', failed: '失败' }
const CHECK_LABEL: Record<CheckStatus, string> = { passed: '通过', uncertain: '不确定', unverified: '未完成检查', failed: '失败' }
const STAGE_LABEL: Record<WorkflowStage, string> = { prepare: '准备源页', recognize: '识别', layout: '恢复布局', render: '生成输出', verify: '自动复核', repair: '局部修复', finalize: '形成结果' }
const OUTPUT_QUALITY_LABEL: Record<string, string> = { auto_passed: '自动通过', completed_with_issues: '带问题完成：存在未可靠转录或源图保留内容', degraded: '部分生成失败，输出不完整' }

function errorText(error: unknown) { return error instanceof Error ? error.message : '发生未知错误' }
function count(value: number | null) { return value == null ? '未知' : value.toLocaleString() }
function snapshotText(run: Run, key: string) { const value = run.settings_snapshot[key]; return typeof value === 'string' ? value : '未记录' }

type Props = {
  detail: BookDetail
  disabled: boolean
  onBusyChange: (busy: boolean) => void
  onRunChange: (run: Run | null) => void
  onBookUpdated: () => void
  onOrganize: () => void
  onSettings: () => void
}
type SourceLocation = { number: number; label: string; bbox: BBox | null; region: string | null; line: string | null }

export default function WorkflowDashboard({ detail, disabled, onBusyChange, onRunChange, onBookUpdated, onOrganize, onSettings }: Props) {
  const bookId = detail.book.id
  const [run, setRun] = useState<Run | null>(null)
  const [loadingRun, setLoadingRun] = useState(true)
  const [runError, setRunError] = useState('')
  const [watchVersion, setWatchVersion] = useState(0)
  const runRef = useRef<Run | null>(null)
  const mounted = useRef(true)
  const startRequest = useRef<{ signature: string; value: RunCreate } | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [settingsError, setSettingsError] = useState('')
  const [model, setModel] = useState('')
  const [paperSize, setPaperSize] = useState<PaperSize>(detail.book.paper_size)
  const [showStart, setShowStart] = useState(false)
  const [scope, setScope] = useState<'all' | 'custom'>('all')
  const [range, setRange] = useState('')
  const [replaceMode, setReplaceMode] = useState<'protect' | 'range'>('protect')
  const [replaceRange, setReplaceRange] = useState('')
  const [requestLimit, setRequestLimit] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [controlling, setControlling] = useState(false)
  const [startError, setStartError] = useState('')
  const [results, setResults] = useState<PageResult[]>([])
  const [issues, setIssues] = useState<Issue[]>([])
  const [resultOffset, setResultOffset] = useState(0)
  const [issueOffset, setIssueOffset] = useState(0)
  const [reading, setReading] = useState(false)
  const [readError, setReadError] = useState('')
  const [readVersion, setReadVersion] = useState(0)
  const [manifest, setManifest] = useState<ExportManifest | null>(null)
  const [manifestError, setManifestError] = useState('')
  const [manifestLoading, setManifestLoading] = useState(false)
  const [savedManifestId, setSavedManifestId] = useState<string | null>(null)
  const [manifestVersion, setManifestVersion] = useState(0)
  const [exporting, setExporting] = useState(false)
  const [location, setLocation] = useState<SourceLocation | null>(null)
  const [imageError, setImageError] = useState(false)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    let cancelled = false
    setSettingsError('')
    api.getSettings().then((value) => {
      if (cancelled) return
      setSettings({ ...value, api_key: '' })
      setModel(value.extraction_model)
    }).catch((error) => { if (!cancelled) setSettingsError(errorText(error)) })
    return () => { cancelled = true }
  }, [bookId])

  useEffect(() => { setPaperSize(detail.book.paper_size) }, [detail.book.paper_size])

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    async function refresh() {
      try {
        const current = runRef.current
        const value = current ? await api.getRun(bookId, current.run_id, controller.signal)
          : (await api.listRuns(bookId, controller.signal))[0] ?? null
        if (controller.signal.aborted) return
        runRef.current = value
        setRun(value)
        onRunChange(value)
        setRunError('')
        setLoadingRun(false)
        if (value && (!current || value.status !== current.status || value.counts.completed !== current.counts.completed)) onBookUpdated()
        if (value && ACTIVE.has(value.status)) timer = window.setTimeout(() => void refresh(), 2000)
      } catch (error) {
        if (!controller.signal.aborted) { setRunError(errorText(error)); setLoadingRun(false) }
      }
    }
    void refresh()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [bookId, watchVersion, onRunChange, onBookUpdated])

  useEffect(() => { setResultOffset(0); setIssueOffset(0); setLocation(null); setSavedManifestId(null) }, [run?.run_id])
  useEffect(() => { setResultOffset(0); setIssueOffset(0); setLocation(null) }, [detail.book.arrangement_revision])
  const resultVersion = `${detail.book.arrangement_revision}:${detail.pages.map((page) => page.current_revision_id ?? '').join(',')}:${run?.run_id ?? ''}:${run?.counts.completed ?? 0}:${run?.status ?? ''}`
  const selectedManifestId = savedManifestId ?? run?.export_manifest_id
  useEffect(() => {
    const controller = new AbortController()
    setReading(true)
    setReadError('')
    setResults([])
    setIssues([])
    Promise.all([
      api.getResults(bookId, resultOffset, PAGE_SIZE, controller.signal),
      api.getIssues(bookId, issueOffset, PAGE_SIZE, controller.signal),
    ]).then(([nextResults, nextIssues]) => {
      if (!controller.signal.aborted) { setResults(nextResults); setIssues(nextIssues) }
    }).catch((error) => { if (!controller.signal.aborted) setReadError(errorText(error)) })
      .finally(() => { if (!controller.signal.aborted) setReading(false) })
    return () => controller.abort()
  }, [bookId, resultOffset, issueOffset, resultVersion, readVersion])

  useEffect(() => {
    const controller = new AbortController()
    setManifest(null)
    setManifestError('')
    const manifestId = selectedManifestId
    if (!manifestId) { setManifestLoading(false); return }
    setManifestLoading(true)
    api.getExportManifest(bookId, manifestId, controller.signal).then((value) => {
      if (!controller.signal.aborted) setManifest(value)
    }).catch((error) => { if (!controller.signal.aborted) setManifestError(errorText(error)) })
      .finally(() => { if (!controller.signal.aborted) setManifestLoading(false) })
    return () => controller.abort()
  }, [bookId, selectedManifestId, run?.status, manifestVersion])

  useEffect(() => { setImageError(false) }, [location])
  const selection = useMemo(() => scope === 'all'
    ? { pages: detail.pages.map((_, index) => index + 1), error: null }
    : resolvePageSelection('custom', range, detail.pages.length), [scope, range, detail.pages])
  const targets = selection.pages.map((position) => detail.pages[position - 1])
  const replacement = replaceMode === 'protect' ? { pages: [], error: null }
    : resolvePageSelection('custom', replaceRange, detail.pages.length)
  const replacementOutsideScope = replacement.pages.some((position) => !selection.pages.includes(position))
  const replaceIds = replacement.pages.filter((position) => detail.pages[position - 1].manual_protected).map((position) => detail.pages[position - 1].page_id)
  const protectedCount = targets.filter((page) => page.manual_protected && !replaceIds.includes(page.page_id)).length
  const limitValue = requestLimit ?? String(3 * targets.length)
  const active = Boolean(run && ACTIVE.has(run.status))
  const unfinished = active || run?.status === 'paused'
  const locked = disabled || starting || controlling || exporting
  const stages = useMemo(() => {
    const counts = new Map<WorkflowStage, number>()
    run?.tasks.filter((task) => task.result_status === null).forEach((task) => counts.set(task.stage, (counts.get(task.stage) ?? 0) + 1))
    return [...counts]
  }, [run])

  async function start() {
    if (!settings || locked || unfinished) return
    const limit = Number(limitValue)
    const scopeError = selection.error || replacement.error || (replacementOutsideScope ? '替换范围必须包含在本轮处理范围中。' : '')
    if (scopeError || !targets.length || !model.trim() || !Number.isInteger(limit) || limit < 0 || !limitValue.trim()) {
      setStartError(scopeError || (!targets.length ? '请选择至少一页。' : !model.trim() ? '请填写识别模型。' : '调用额度必须为大于或等于 0 的整数。'))
      return
    }
    const payload = { page_ids: targets.map((page) => page.page_id), expected_arrangement_revision: detail.book.arrangement_revision,
      policy: { replace_page_ids: replaceIds }, request_limit: limit }
    const signature = JSON.stringify({ ...payload, model: model.trim(), paperSize })
    const previous = startRequest.current
    const request: RunCreate = previous?.signature === signature ? previous.value : { ...payload, client_request_id: crypto.randomUUID() }
    startRequest.current = { signature, value: request }
    setStarting(true)
    onBusyChange(true)
    setStartError('')
    try {
      const latestSettings = await api.getSettings()
      if (!mounted.current) return
      setSettings({ ...latestSettings, api_key: '' })
      if (model.trim() !== latestSettings.extraction_model) {
        const saved = await api.saveSettings({ ...latestSettings, extraction_model: model.trim(), api_key: '' }, false)
        if (!mounted.current) return
        setSettings({ ...saved, api_key: '' })
      }
      if (paperSize !== detail.book.paper_size) await api.saveBookLayout(bookId, { paper_size: paperSize })
      if (!mounted.current) return
      const value = await api.createRun(bookId, request)
      if (!mounted.current) return
      runRef.current = value
      setRun(value)
      onRunChange(value)
      startRequest.current = null
      setShowStart(false)
      setWatchVersion((version) => version + 1)
      onBookUpdated()
    } catch (error) {
      if (mounted.current) { setStartError(errorText(error)); onBookUpdated() }
    } finally {
      if (mounted.current) { setStarting(false); onBusyChange(false) }
    }
  }

  async function control(action: 'pause' | 'resume') {
    if (!run || locked) return
    setControlling(true)
    onBusyChange(true)
    try {
      const value = await (action === 'pause' ? api.pauseRun(bookId, run.run_id) : api.resumeRun(bookId, run.run_id))
      if (!mounted.current) return
      runRef.current = value
      setRun(value)
      onRunChange(value)
      setWatchVersion((version) => version + 1)
      setRunError('')
      onBookUpdated()
    } catch (error) { if (mounted.current) setRunError(errorText(error)) }
    finally { if (mounted.current) { setControlling(false); onBusyChange(false) } }
  }

  async function createSavedExport() {
    if (locked || unfinished) return
    setExporting(true)
    onBusyChange(true)
    setManifestError('')
    try {
      const value = await api.createExportManifest(bookId, { expected_arrangement_revision: detail.book.arrangement_revision })
      if (mounted.current) setSavedManifestId(value.manifest_id)
    } catch (error) { if (mounted.current) setManifestError(errorText(error)) }
    finally { if (mounted.current) { setExporting(false); onBusyChange(false) } }
  }

  async function download(format: 'pdf' | 'partial_pdf' | 'latex' | 'json') {
    if (!manifest || locked) return
    setExporting(true)
    onBusyChange(true)
    setManifestError('')
    try {
      const output = await api.downloadManifest(bookId, manifest.manifest_id, format)
      if (mounted.current) downloadBlob(safeFilename(detail.book.title) + (format === 'partial_pdf' ? '（部分输出）' : '') + output.extension, output.blob)
    } catch (error) { if (mounted.current) setManifestError(errorText(error)) }
    finally { if (mounted.current) { setExporting(false); onBusyChange(false) } }
  }

  function locate(pageId: string, bbox: BBox | null = null, region: string | null = null, line: string | null = null) {
    const page = detail.pages.find((item) => item.page_id === pageId)
    const entry = manifest?.pages.find((item) => item.page_id === pageId)
    if (!page && !entry) return
    setLocation({ number: page?.number ?? entry!.page_number, label: page ? `${page.source_filename} · 源第 ${page.source_page} 页`
      : `${entry!.source_filename} · 源第 ${entry!.source_page} 页`, bbox, region, line })
  }

  const complete = Boolean(manifest?.complete && manifest.outputs.complete !== 'false')
  const outputQuality = manifest?.outputs.quality
  const outputHasIssues = Boolean(outputQuality && outputQuality !== 'auto_passed')
  const failedPages = manifest?.pages.filter((page) => page.result_status === 'failed').map((page) => `${page.position + 1}（${page.source_filename} 源第 ${page.source_page} 页）`) ?? []
  const pdfFormat = manifest?.outputs.partial_pdf ? 'partial_pdf' : manifest?.outputs.pdf ? 'pdf' : null

  return <section className="workflow-dashboard" aria-label="自动任务总览">
    <header className="dashboard-heading"><div><h2>自动任务总览</h2><p>一次开始后自动处理、复核和生成结果。上传资料不会发送模型请求。</p></div>
      <div className="button-row"><button onClick={onOrganize} disabled={locked || unfinished}>调整页序（可选）</button>
        {run && !unfinished && <button className="primary" onClick={() => { setShowStart(true); setStartError('') }} disabled={locked || loadingRun}>开始新任务</button>}</div></header>
    {runError && <div className="error-panel" role="alert"><span>{runError}</span><button onClick={() => setWatchVersion((version) => version + 1)}>重新读取任务</button></div>}
    {loadingRun ? <p role="status">正在读取任务…</p> : run && <section className="run-overview" aria-label="当前任务进度">
      <div className="run-title"><h3>{RUN_LABEL[run.status]}</h3><span className="run-id">{run.run_id}</span>
        {active && <button onClick={() => void control('pause')} disabled={locked || run.status === 'pausing'}>{run.status === 'pausing' ? '正在暂停…' : '暂停任务'}</button>}
        {(run.status === 'paused' || run.status === 'interrupted') && <button className="primary" onClick={() => void control('resume')} disabled={locked}>恢复同一任务</button>}</div>
      <div className="progress-track" role="progressbar" aria-label="自动处理进度" aria-valuemin={0} aria-valuemax={run.counts.total} aria-valuenow={run.counts.completed}><span style={{ width: `${run.counts.total ? run.counts.completed / run.counts.total * 100 : 0}%` }} /></div>
      <div className="run-counts"><span>已完成 <strong>{run.counts.completed} / {run.counts.total}</strong></span><span>自动通过 <strong>{run.counts.auto_passed}</strong></span><span>带问题完成 <strong>{run.counts.completed_with_issues}</strong></span><span>失败 <strong>{run.counts.failed}</strong></span></div>
      <p className="run-stages">{stages.length ? stages.map(([stage, amount]) => `${STAGE_LABEL[stage]} ${amount} 页`).join(' · ') : '本轮页面已形成终态'}{run.status === 'paused' && ' · 恢复会沿用原范围、模型、纸型和额度'}</p>
      <dl className="run-facts"><div><dt>模型</dt><dd>{snapshotText(run, 'extraction_model')}</dd></div><div><dt>纸型 / 页序</dt><dd>{snapshotText(run, 'paper_size')} · r{run.arrangement_revision}</dd></div><div><dt>物理请求</dt><dd>{run.request_count} / {run.request_limit} 次</dd></div><div><dt>已知 tokens</dt><dd>输入 {count(run.usage.input_tokens)} · 输出 {count(run.usage.output_tokens)} · 合计 {count(run.usage.total_tokens)}{!run.usage.complete && '（用量不完整）'}</dd></div></dl>
      {run.error && <div className="error-panel" role="alert"><strong>任务错误</strong><span>{run.error}</span></div>}
      <p className="dashboard-note">普通内容疑点会自动继续其他页面；未可靠转录的区域保留源内容并标记。暂停后已发送请求会结算，不启动新阶段。</p>
    </section>}

    {!loadingRun && !unfinished && (!run || showStart) && <section className="run-start" aria-label="开始处理设置">
      <h3>开始自动处理</h3>
      <div className="run-start-fields"><label><span>处理范围</span><select value={scope} onChange={(event) => { setScope(event.target.value as 'all' | 'custom'); setRequestLimit(null) }} disabled={locked}><option value="all">当前页序全部 {detail.pages.length} 页</option><option value="custom">指定编排页码</option></select></label>
        {scope === 'custom' && <label><span>编排页码</span><input value={range} onChange={(event) => { setRange(event.target.value); setRequestLimit(null) }} placeholder="例如 1-20,25" disabled={locked} /></label>}
        <label><span>识别模型</span><input value={model} onChange={(event) => setModel(event.target.value)} placeholder="填写模型名称" disabled={locked || !settings} /></label>
        <label><span>输出纸型</span><select value={paperSize} onChange={(event) => setPaperSize(event.target.value as PaperSize)} disabled={locked}>{Object.entries(PAPER_SIZES).map(([size, paper]) => <option key={size} value={size}>{paper.label}</option>)}</select></label>
        <label><span>物理请求额度</span><input type="number" min="0" step="1" value={limitValue} onChange={(event) => setRequestLimit(event.target.value)} disabled={locked} /><small>默认 3 × 本轮页数；失败请求也计数。</small></label>
        <label><span>已有人工稿</span><select value={replaceMode} onChange={(event) => setReplaceMode(event.target.value as 'protect' | 'range')} disabled={locked}><option value="protect">保护已有人工稿</option><option value="range">允许替换指定人工稿范围</option></select></label>
        {replaceMode === 'range' && <label><span>允许替换的编排页码</span><input value={replaceRange} onChange={(event) => setReplaceRange(event.target.value)} placeholder="例如 3,8-12" disabled={locked} /><small>必须属于本轮范围，仅替换其中的人工稿。</small></label>}</div>
      <p className="dashboard-note">当前范围 {targets.length} 页；保留 {protectedCount} 页人工稿，明确允许替换 {replaceIds.length} 页。保留稿不计作本轮自动通过。成功新结果按启动授权自动采用并保留旧修订；运行中保存的新编辑不会被旧候选覆盖。</p>
      <p className="dashboard-note">开始时冻结页序、模型、纸型及额度。不需要逐页批准、填写坐标或主动测试连接。启动响应未收到时，以相同设置重试会读取同一任务。</p>
      {(startError || settingsError) && <div className="error-panel" role="alert"><span>{startError || settingsError}</span></div>}
      <div className="button-row"><button className="primary" onClick={() => void start()} disabled={locked || !settings || !detail.pages.length}>{starting ? '正在启动…' : `开始自动处理 ${targets.length} 页`}</button><button onClick={onSettings} disabled={locked}>模型接入设置</button>{run && <button onClick={() => setShowStart(false)} disabled={locked}>收起</button>}</div>
    </section>}

    <section className="workflow-output" aria-label="自动输出">
      <div className="dashboard-section-heading"><div><h3>输出</h3><p>PDF、LaTeX 资源包与 JSON 使用同一份页序及修订清单。</p></div><button onClick={() => void createSavedExport()} disabled={locked || unfinished || !detail.pages.length}>{exporting ? '正在生成/下载…' : '从当前已保存稿生成新输出'}</button></div>
      {manifestLoading && <p role="status">正在读取自动生成的输出…</p>}
      {manifestError && <div className="error-panel" role="alert"><span>{manifestError}</span><button onClick={() => setManifestVersion((version) => version + 1)}>重新读取</button></div>}
      {manifest ? <>
        <p className={complete ? 'output-complete' : 'output-incomplete'}>{complete ? '输出范围完整' : '部分输出：输出不完整'} · {manifest.pages.length} 个源页 · 页序 r{manifest.arrangement_revision}{manifest.issues.length > 0 && ` · ${manifest.issues.length} 条问题说明`}</p>
        <p className="dashboard-note">{manifest.run_id ? '本轮冻结任务的输出' : '当前已保存稿生成的输出'} · 清单 {manifest.manifest_id} · 输出设置 v{manifest.output_settings_version}。之后的编辑不会改变这份下载快照。</p>
        <p className={outputHasIssues || !outputQuality ? 'output-incomplete' : 'output-complete'}>输出质量：{outputQuality ? OUTPUT_QUALITY_LABEL[outputQuality] ?? outputQuality : '未记录自动结论'}。源图保留不计作成功文字化；完整范围也可能带问题完成。</p>
        {manifest.outputs.error && <p className="output-incomplete">生成说明：{manifest.outputs.error}</p>}
        {!complete && <p className="output-incomplete">{manifest.outputs.missing_pages ? `缺失输出页：${manifest.outputs.missing_pages}` : failedPages.length ? `未完成页面：${failedPages.join('、')}` : '有页面未形成完整输出；缺页和生成诊断见 JSON 清单。'}</p>}
        <div className="button-row"><button onClick={() => void download(pdfFormat!)} disabled={locked || !pdfFormat}>{pdfFormat === 'partial_pdf' || !complete ? '下载部分 PDF' : '下载 PDF'}</button><button onClick={() => void download('latex')} disabled={locked || !manifest.outputs.latex}>下载 LaTeX 资源包</button><button onClick={() => void download('json')} disabled={locked || !manifest.outputs.json}>下载 JSON 清单</button></div>
        {pdfFormat && <details className="workflow-pdf"><summary>{complete ? '查看自动生成的 PDF' : '查看部分 PDF（输出不完整）'}</summary><iframe title={complete ? '自动输出 PDF' : '部分输出 PDF'} src={api.manifestDownloadUrl(bookId, manifest.manifest_id, pdfFormat)} /></details>}
      </> : !manifestLoading && <p className="dashboard-note">{unfinished ? '任务结束后自动提供输出，无需手动编译。' : run ? '本轮尚未生成可用输出。已有稿件可主动生成新输出。' : '开始任务后自动生成输出；已有人工稿也可主动导出。'}</p>}
    </section>

    <section className="workflow-results" aria-label="当前采用结果">
      <div className="dashboard-section-heading"><div><h3>当前采用结果</h3><p>执行完成和质量通过分别记录；受保护人工稿或未采用候选不会冒充自动通过。</p></div><button onClick={() => { setReadVersion((version) => version + 1); onBookUpdated() }} disabled={reading}>刷新结果</button></div>
      {readError && <div className="error-panel" role="alert"><span>{readError}</span></div>}
      {reading ? <p role="status">正在读取结果和问题说明…</p> : <div className="workflow-table"><table><thead><tr><th>来源</th><th>结果</th><th>内容 / 布局 / 覆盖</th><th>自动处置</th></tr></thead><tbody>{results.map((result) => {
        const page = detail.pages.find((item) => item.page_id === result.page_id)
        return <tr key={result.page_id}><td><button className="source-location-link" onClick={() => locate(result.page_id)}>{page ? `${page.source_filename} · 源第 ${page.source_page} 页` : `源页面 ${result.page_number}`}</button></td><td>{result.result_status ? RESULT_LABEL[result.result_status] : '未形成自动结论'}</td><td>{CHECK_LABEL[result.content]} / {CHECK_LABEL[result.layout]} / {CHECK_LABEL[result.coverage]}</td><td>{result.source_disposition === 'source_page_preserved' ? '保留源页（未可靠转录）' : result.source_disposition === 'regions_preserved' ? '保留源区域（未可靠转录）' : result.source_disposition === 'transcribed' ? '转录候选' : '保留已保存稿'}</td></tr>
      })}</tbody></table>{!results.length && <p className="dashboard-note">当前页暂无结果。</p>}</div>}
      <div className="workflow-pagination"><button onClick={() => setResultOffset(Math.max(0, resultOffset - PAGE_SIZE))} disabled={reading || resultOffset === 0}>上一页</button><span>第 {resultOffset / PAGE_SIZE + 1} 页 · 每页最多 {PAGE_SIZE} 条</span><button onClick={() => setResultOffset(resultOffset + PAGE_SIZE)} disabled={reading || results.length < PAGE_SIZE || resultOffset + PAGE_SIZE >= detail.pages.length}>下一页</button></div>
    </section>

    <section className="workflow-issues" aria-label="只读问题说明"><h3>问题说明</h3><p className="dashboard-note">这里只说明自动处理结果与源位置，不是待办。无需处理这些说明，任务和输出仍会自动完成。</p>
      {!reading && !issues.length && <p className="dashboard-note">当前页没有问题说明；这不代表未检查的页面已经通过。</p>}
      <ul>{issues.map((issue) => <li key={issue.issue_id}><div><strong>{issue.reason}</strong><span>{issue.category} · {issue.severity === 'error' ? '错误' : issue.severity === 'warning' ? '提醒' : '说明'}</span></div><p>自动处置：{issue.disposition}</p><button onClick={() => locate(issue.page_id, issue.source_bbox, issue.region_id, issue.line_id)}>查看来源{issue.region_id ? ` · 区域 ${issue.region_id}` : ''}{issue.line_id ? ` · 行 ${issue.line_id}` : ''}</button></li>)}</ul>
      <div className="workflow-pagination"><button onClick={() => setIssueOffset(Math.max(0, issueOffset - PAGE_SIZE))} disabled={reading || issueOffset === 0}>上一页</button><span>第 {issueOffset / PAGE_SIZE + 1} 页 · 每页最多 {PAGE_SIZE} 条</span><button onClick={() => setIssueOffset(issueOffset + PAGE_SIZE)} disabled={reading || issues.length < PAGE_SIZE}>下一页</button></div>
    </section>
    {location && <section className="workflow-source" aria-label="问题源位置"><div className="dashboard-section-heading"><h3>{location.label}</h3><button onClick={() => setLocation(null)}>收起来源</button></div><p className="dashboard-note">{location.region && `区域 ${location.region} · `}{location.line && `行 ${location.line} · `}{location.bbox ? '标框为自动定位的源区域。' : '未提供区域坐标，显示完整源页。'}</p>{imageError ? <p role="alert">源页预览未载入。原文件仍保存在项目中。</p> : <div className="workflow-source-image"><img src={api.pagePreviewUrl(bookId, location.number)} alt={location.label} onError={() => setImageError(true)} />{location.bbox && <span className="comparison-highlight" style={{ left: `${location.bbox[0] * 100}%`, top: `${location.bbox[1] * 100}%`, width: `${(location.bbox[2] - location.bbox[0]) * 100}%`, height: `${(location.bbox[3] - location.bbox[1]) * 100}%` }} />}</div>}</section>}
  </section>
}
