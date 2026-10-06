import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Notice, SelectionDraft, SourceFile, SourcePageSummary } from './types'
import './organizer.css'

const SIZE = 24
type Props = { bookId: string; files: SourceFile[]; selection: SelectionDraft; total: number; onSaved: (draft: SelectionDraft) => void; onRefreshed: (draft: SelectionDraft) => void; onNotice: (notice: Notice) => void; onDirtyChange: (dirty: boolean) => void }
function parseRange(text: string, maximum: number) {
  const result = new Set<number>()
  for (const token of text.replaceAll('，', ',').split(',')) {
    const match = /^(\d+)(?:\s*[-—]\s*(\d+))?$/.exec(token.trim())
    if (!match) throw new Error('请输入源文件页码，例如 1-5,8。')
    const first = Number(match[1]), last = Number(match[2] ?? match[1])
    if (first < 1 || last < first || last > maximum) throw new Error(`页码范围必须在 1—${maximum} 内。`)
    for (let page = first; page <= last; page++) result.add(page)
  }
  return result
}
function SourceImage({ bookId, page }: { bookId: string; page: SourcePageSummary }) {
  const [failed, setFailed] = useState(false), [retry, setRetry] = useState(0)
  return failed ? <div className="selection-image-error">预览未载入<button onClick={() => { setFailed(false); setRetry(retry + 1) }}>重试预览</button></div> : <img src={`${api.sourcePreviewUrl(bookId, page.page_id, undefined, page.source_version)}&retry=${retry}`} alt={`${page.source_filename}，第 ${page.source_page} 页`} loading="lazy" decoding="async" onError={() => setFailed(true)} />
}
export default function ProjectOrganizer({ bookId, files, selection, total, onSaved, onRefreshed, onNotice, onDirtyChange }: Props) {
  const [ids, setIds] = useState(selection.page_ids), [sourceId, setSourceId] = useState(files[0]?.id ?? '')
  const [offset, setOffset] = useState(0), [sequence, setSequence] = useState(false), [range, setRange] = useState('')
  const [visible, setVisible] = useState<SourcePageSummary[]>([]), [sourceTotal, setSourceTotal] = useState(0)
  const [metadata, setMetadata] = useState<Record<string, SourcePageSummary>>({})
  const cache = useRef<Record<string, SourcePageSummary>>({}), complete = useRef(false)
  const customOrder = useRef<boolean | null>(null)
  const sourceOrder = new Map(files.map((file) => [file.id, file.position]))
  const compareSources = (a: string, b: string, pages: Record<string, SourcePageSummary>) => {
    const first = pages[a], second = pages[b]
    if (!first || !second) return first ? -1 : second ? 1 : 0
    return (sourceOrder.get(first.source_id) ?? Infinity) - (sourceOrder.get(second.source_id) ?? Infinity) || first.source_page - second.source_page || first.number - second.number
  }
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [reload, setReload] = useState(0)
  const [preview, setPreview] = useState<SourcePageSummary | null>(null), [zoom, setZoom] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null), lifetime = useRef<AbortController | null>(null)
  useEffect(() => { const controller = new AbortController(); lifetime.current = controller; return () => controller.abort() }, [])
  const sourceIdentity = files.map((file) => `${file.id}:${file.page_count}`).join('|')
  useEffect(() => { cache.current = {}; complete.current = false; setMetadata({}); setOffset(0); setReload((value) => value + 1) }, [bookId, total, sourceIdentity, selection.selection_revision])
  useEffect(() => { onDirtyChange(ids.length !== selection.page_ids.length || ids.some((id, index) => id !== selection.page_ids[index])); return () => onDirtyChange(false) }, [ids, selection.page_ids, onDirtyChange])
  useEffect(() => {
    if (sequence) return
    const controller = new AbortController(); setLoading(true); setError('')
    api.getSourcePages(bookId, offset, SIZE, sourceId || undefined, controller.signal).then((value) => {
      if (controller.signal.aborted) return
      setVisible(value.items); setSourceTotal(value.total)
      cache.current = { ...cache.current, ...Object.fromEntries(value.items.map((page) => [page.page_id, page])) }; setMetadata(cache.current)
    }).catch((cause: unknown) => { if (!controller.signal.aborted) { setVisible([]); setError(cause instanceof Error ? cause.message : '无法读取来源页') } }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [bookId, offset, sourceId, reload, sequence])
  useEffect(() => { if (preview) { setZoom(false); dialog.current?.showModal() } else dialog.current?.close() }, [preview])
  const ensureAll = async () => {
    if (complete.current) return Object.values(cache.current)
    const pages: Record<string, SourcePageSummary> = {}; let position = 0, count = total
    while (position < count) {
      const value = await api.getSourcePages(bookId, position, 200, undefined, lifetime.current?.signal)
      if (!value.items.length && position < value.total) throw new Error('来源已变化，请刷新选页。')
      value.items.forEach((page) => { pages[page.page_id] = page }); position += value.items.length; count = value.total
    }
    if (lifetime.current?.signal.aborted) throw new Error('已离开选页')
    cache.current = pages; setMetadata(pages); complete.current = true
    if (customOrder.current === null) { const ordered = [...selection.page_ids].sort((a, b) => compareSources(a, b, pages)); customOrder.current = ordered.some((id, index) => id !== selection.page_ids[index]) }
    return Object.values(pages)
  }
  const action = async (work: () => Promise<void>) => {
    setBusy(true); setError('')
    try { await work() } catch (cause) { if (!lifetime.current?.signal.aborted) setError(cause instanceof Error ? cause.message : '操作失败') }
    finally { if (!lifetime.current?.signal.aborted) setBusy(false) }
  }
  const include = (pages: SourcePageSummary[]) => setIds((current) => { const next = [...current, ...pages.map((page) => page.page_id).filter((id) => !current.includes(id))]; return customOrder.current ? next : next.sort((a, b) => compareSources(a, b, cache.current)) })
  const move = (index: number, target: number) => { customOrder.current = true; setIds((current) => { const next = [...current]; const [id] = next.splice(index, 1); next.splice(target, 0, id); return next }) }
  const next = () => action(async () => {
    const pages = await ensureAll(), versions = Object.fromEntries(pages.filter((page) => ids.includes(page.page_id)).map((page) => [page.page_id, page.source_version]))
    if (Object.keys(versions).length !== ids.length) throw new Error('部分所选来源已变化，请刷新选页。')
    const saved = await api.saveSelection(bookId, { page_ids: ids, source_versions: versions, expected_selection_revision: selection.selection_revision }); onNotice(null); onSaved(saved)
  })
  const pageTotal = sequence ? ids.length : sourceTotal
  return <section className="selection-page" aria-busy={busy}>
    <header className="section-heading"><p className="eyebrow">第二步</p><h1>选页</h1><p>保留源文件顺序，或调整已选页的阅读顺序。</p></header>
    {!selection.valid && <p className="inline-result error-box">来源已有变化，保存选择时会使用当前来源版本。</p>}
    <div className="selection-tools"><label>来源文件<select disabled={busy} value={sourceId} onChange={(event) => { setSourceId(event.target.value); setOffset(0); setSequence(false) }}><option value="">全部来源</option>{files.map((file) => <option key={file.id} value={file.id}>{file.filename} · {file.page_count} 页</option>)}</select></label><button disabled={busy} onClick={() => action(async () => include(await ensureAll()))}>全选全部来源</button><button disabled={busy || !ids.length} onClick={() => { setIds([]); customOrder.current = false; setOffset(0) }}>取消全部</button><button disabled={busy} aria-pressed={sequence} onClick={() => action(async () => { if (!sequence) await ensureAll(); setSequence(!sequence); setOffset(0) })}>{sequence ? '查看来源缩略图' : '调整已选顺序'}</button></div>
    {!sequence && <form className="selection-range" onSubmit={(event) => { event.preventDefault(); void action(async () => { const file = files.find((item) => item.id === sourceId); if (!file) throw new Error('先选择一个来源文件。'); const wanted = parseRange(range, file.page_count); include((await ensureAll()).filter((page) => page.source_id === sourceId && wanted.has(page.source_page))) }) }}><label>范围选择<input value={range} onChange={(event) => setRange(event.target.value)} placeholder="源文件页码，例如 1-5,8" disabled={busy} /></label><button disabled={busy || !sourceId || !range.trim()}>加入选择</button></form>}
    {error && <div className="inline-result error-box" role="alert">{error}<button disabled={busy} onClick={() => action(async () => { const current = await api.getSelection(bookId); setIds(current.page_ids); customOrder.current = null; complete.current = false; setReload((value) => value + 1); onRefreshed(current) })}>刷新列表</button></div>}
    {sequence ? <ol className="selection-sequence" start={offset + 1}>{ids.slice(offset, offset + SIZE).map((id, index) => { const page = metadata[id], position = offset + index; return <li key={id}><span><strong>{position + 1}.</strong> {page ? `${page.source_filename} · 第 ${page.source_page} 页` : '来源页待刷新'}</span><div><button disabled={busy || position === 0} onClick={() => move(position, position - 1)} aria-label="前移一页">↑</button><button disabled={busy || position === ids.length - 1} onClick={() => move(position, position + 1)} aria-label="后移一页">↓</button><button disabled={busy || position === 0} onClick={() => move(position, 0)}>移至开头</button><button disabled={busy} onClick={() => { setIds(ids.filter((item) => item !== id)); setOffset(Math.min(offset, Math.floor(Math.max(0, ids.length - 2) / SIZE) * SIZE)) }}>移除</button></div></li> })}</ol> : loading ? <p className="center-state">正在读取来源页…</p> : files.filter((file) => visible.some((page) => page.source_id === file.id)).map((file) => <section key={file.id} className="selection-source"><h2>{file.filename}</h2><div className="selection-grid">{visible.filter((page) => page.source_id === file.id).map((page) => <article key={page.page_id} className={ids.includes(page.page_id) ? 'selection-card selected' : 'selection-card'}><div className="selection-thumb"><SourceImage bookId={bookId} page={page} /></div><label><input type="checkbox" disabled={busy} checked={ids.includes(page.page_id)} onChange={() => { if (ids.includes(page.page_id)) setIds((current) => current.filter((id) => id !== page.page_id)); else void action(async () => { await ensureAll(); include([page]) }) }} />第 {page.source_page} 页</label><button onClick={() => setPreview(page)}>放大预览</button></article>)}</div></section>)}
    <div className="selection-pagination"><button disabled={busy || !offset} onClick={() => setOffset(offset - SIZE)}>上一组</button><span>{pageTotal ? offset + 1 : 0}—{Math.min(pageTotal, offset + SIZE)} / {pageTotal}</span><button disabled={busy || offset + SIZE >= pageTotal} onClick={() => setOffset(offset + SIZE)}>下一组</button></div>
    <footer className="selection-footer"><div><strong>已选择 {ids.length} / {total} 页</strong><span>{ids.length ? '下一步仅保存选择，开始识别前不会调用模型。' : '请至少选择一页。'}</span></div><button className="primary" disabled={busy || !ids.length} onClick={next}>{busy ? '正在保存…' : `下一步：识别 ${ids.length} 页`}</button></footer>
    <dialog ref={dialog} className="selection-preview" onCancel={() => setPreview(null)}><header><strong>{preview?.source_filename} · 第 {preview?.source_page} 页</strong><button onClick={() => setPreview(null)}>关闭</button></header>{preview && <div className={zoom ? 'selection-preview-image zoomed' : 'selection-preview-image'}><SourceImage key={preview.page_id} bookId={bookId} page={preview} /></div>}<button onClick={() => setZoom(!zoom)}>{zoom ? '适合页面' : '放大显示'}</button></dialog>
  </section>
}
