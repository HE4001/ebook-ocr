import { useEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent } from 'react'
import { api } from './api'
import { resolvePageSelection, type ProcessScope } from './pageSelection'
import { fileDepth, fileSubtree, findFilePageAnchor, flattenFileParents, flattenFiles, insertPageBlock, moveFileTree, reorderVisiblePages, type FileParents } from './organizerOrder'
import { useOrganizerMotion } from './useOrganizerMotion'
import type { Arrangement, BookDetail, Notice, Page } from './types'
import './organizer.css'

type Props = {
  bookId: string
  onSaved: (detail: BookDetail) => void
  onNotice: (notice: Notice) => void
  onDirtyChange?: (dirty: boolean) => void
  onBusyChange?: (busy: boolean) => void
  disabled?: boolean
}
type View = 'source' | 'sequence'
type Side = 'before' | 'after'
type Dragged = { kind: 'file'; id: string } | { kind: 'page'; id: number }
type DropTarget = { key: string; side: Side | 'inside'; hideIndicator?: boolean }
const PAGE_SIZE = 12
const CHILD_FILE_HELP = '子文件不能再嵌入文件，请选择顶层文件。'

function signature(files: string[], parents: FileParents, pages: number[]) {
  return JSON.stringify([files, files.map((id) => parents[id]), pages])
}

function PreviewImage({ bookId, page, eager = false }: { bookId: string; page: Page; eager?: boolean }) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  if (failed) return <span className="organizer-image-error">预览未载入<button type="button" onClick={(event) => {
    event.stopPropagation()
    setAttempt(attempt + 1)
    setFailed(false)
  }}>重试预览</button></span>
  return <img src={`${api.pagePreviewUrl(bookId, page.number)}${attempt ? `?retry=${attempt}` : ''}`}
    alt={`${page.source_filename}，源第 ${page.source_page} 页`} draggable={false}
    loading={eager ? 'eager' : 'lazy'} decoding="async" onError={() => setFailed(true)} />
}

export default function ProjectOrganizer({ bookId, onSaved, onNotice, onDirtyChange, onBusyChange, disabled = false }: Props) {
  const [arrangement, setArrangement] = useState<Arrangement | null>(null)
  const [loadError, setLoadError] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [fileOrder, setFileOrder] = useState<string[]>([])
  const [parents, setParents] = useState<FileParents>({})
  const [layout, setLayout] = useState<number[]>([])
  const [included, setIncluded] = useState<Set<number>>(new Set())
  const [savedSignature, setSavedSignature] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [view, setView] = useState<View>('source')
  const [pageIndex, setPageIndex] = useState(0)
  const [scope, setScope] = useState<ProcessScope | 'all'>('all')
  const [range, setRange] = useState('')
  const [rangeError, setRangeError] = useState('')
  const [previewId, setPreviewId] = useState<number | null>(null)
  const [zoomed, setZoomed] = useState(false)
  const [moving, setMoving] = useState<number[]>([])
  const [insertTarget, setInsertTarget] = useState('1')
  const [insertSide, setInsertSide] = useState<Side>('before')
  const [insertError, setInsertError] = useState('')
  const [embedParent, setEmbedParent] = useState('')
  const [embedFile, setEmbedFile] = useState('')
  const [embedScope, setEmbedScope] = useState<'' | 'selected' | 'all'>('')
  const [embedAnchor, setEmbedAnchor] = useState<number | null>(null)
  const [embedSide, setEmbedSide] = useState<Side>('after')
  const [embedError, setEmbedError] = useState('')
  const [saving, setSaving] = useState(false)
  const [dragged, setDragged] = useState<Dragged | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [dropped, setDropped] = useState<string[]>([])
  const [liveMessage, setLiveMessage] = useState('拖动后，页面顺序即时更新')
  const previewDialog = useRef<HTMLDialogElement>(null)
  const insertDialog = useRef<HTMLDialogElement>(null)
  const fileDialog = useRef<HTMLDialogElement>(null)
  const dragging = useRef<Dragged | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoadError('')
    api.getArrangement(bookId).then((result) => {
      if (cancelled) return
      const nextParents = flattenFileParents(Object.fromEntries(result.files.map((file) => [file.id, file.parent_id ?? null])))
      const files = flattenFiles([...result.files].sort((a, b) => a.position - b.position).map((file) => file.id), nextParents)
      const selected = new Set(result.order)
      const remaining = files.flatMap((file) => result.pages.filter((page) => page.source_id === file && !selected.has(page.number))
        .sort((a, b) => a.source_page - b.source_page).map((page) => page.number))
      setArrangement(result)
      setFileOrder(files)
      setParents(nextParents)
      setLayout([...result.order, ...remaining])
      setIncluded(selected)
      setSourceId(files[0] ?? '')
      setSavedSignature(signature(files, nextParents, result.order))
    }).catch((error: unknown) => {
      if (!cancelled) setLoadError(error instanceof Error ? error.message : '页面编排读取失败')
    })
    return () => { cancelled = true }
  }, [bookId, loadAttempt])

  const pageById = useMemo(() => new Map(arrangement?.pages.map((page) => [page.number, page])), [arrangement])
  const fileById = useMemo(() => new Map(arrangement?.files.map((file) => [file.id, file])), [arrangement])
  const order = useMemo(() => layout.filter((id) => included.has(id)), [layout, included])
  const positions = useMemo(() => new Map(order.map((id, index) => [id, index + 1])), [order])
  const sourceTree = useMemo(() => fileSubtree(sourceId, fileOrder, parents), [sourceId, fileOrder, parents])
  const sourcePages = useMemo(() => layout.filter((id) => sourceTree.has(pageById.get(id)!.source_id)), [layout, sourceTree, pageById])
  const visibleIds = view === 'source' ? sourcePages : order
  const selectedIds = visibleIds.filter((id) => included.has(id))
  const dirty = !!arrangement && signature(fileOrder, parents, order) !== savedSignature
  const busy = disabled || saving
  const source = fileById.get(sourceId)
  const lastPage = Math.max(0, Math.ceil(visibleIds.length / PAGE_SIZE) - 1)
  const currentPage = Math.min(pageIndex, lastPage)
  const displayedIds = visibleIds.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  const previewPage = previewId === null ? undefined : pageById.get(previewId)
  const previewPosition = previewId === null ? -1 : visibleIds.indexOf(previewId)
  const insertHasAnchor = order.some((id) => !moving.includes(id))
  const anchorPage = pageById.get(order[Number(insertTarget) - 1])
  const embedTree = fileSubtree(embedFile, fileOrder, parents)
  const embedPages = layout.filter((id) => embedTree.has(pageById.get(id)!.source_id))
  const embedSelectedCount = embedPages.filter((id) => included.has(id)).length
  const embedParentTree = fileSubtree(embedParent, fileOrder, parents)
  const embedTargets = layout.filter((id) => embedParentTree.has(pageById.get(id)!.source_id) && !embedTree.has(pageById.get(id)!.source_id))
  const embedCandidates = fileOrder.filter((id) => id !== embedParent && !fileSubtree(id, fileOrder, parents).has(embedParent))
  const motionRef = useOrganizerMotion(JSON.stringify([fileOrder, fileOrder.map((id) => parents[id]), displayedIds]), `${view}:${sourceId}:${currentPage}`)

  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!dropped.length) return
    const timer = window.setTimeout(() => setDropped([]), 520)
    return () => window.clearTimeout(timer)
  }, [dropped])

  function announce(message: string, keys: string[] = []) {
    setLiveMessage(message)
    setDropped(keys)
  }

  function chooseSource(id: string) {
    setSourceId(id)
    setView('source')
    setPageIndex(0)
    setRangeError('')
  }

  function chooseView(next: View) {
    setView(next)
    setPageIndex(0)
  }

  function reorderPage(from: number, to: number) {
    if (from === to) return
    setLayout(reorderVisiblePages(layout, visibleIds, from, to))
    announce('页面顺序已更新', [`page:${visibleIds[from]}`])
  }

  function selectRange() {
    const result = scope === 'all'
      ? { pages: Array.from({ length: source!.page_count }, (_, index) => index + 1), error: null }
      : resolvePageSelection(scope, range, source!.page_count)
    setRangeError(result.error ?? '')
    if (result.error) return
    const numbers = new Set(result.pages)
    const next = new Set(included)
    layout.forEach((id) => {
      const page = pageById.get(id)!
      if (page.source_id === sourceId) {
        if (numbers.has(page.source_page)) next.add(id)
        else next.delete(id)
      }
    })
    setIncluded(next)
    announce(`当前文件已勾选 ${result.pages.length} 页`)
  }

  function toggleIncluded(id: number) {
    setIncluded((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function movePagesToEnd(ids: number[]) {
    setLayout(insertPageBlock(layout, ids, null))
    announce(`已将 ${ids.length} 页移至末尾`, ids.map((id) => `page:${id}`))
  }

  function removePages(ids: number[]) {
    const removed = new Set(ids)
    setIncluded(new Set([...included].filter((id) => !removed.has(id))))
    announce(`已取消当前视图 ${ids.length} 页的勾选`)
  }

  function openInsert(ids: number[]) {
    setMoving(ids)
    const firstOther = order.findIndex((id) => !ids.includes(id))
    setInsertTarget(String(firstOther === -1 ? 1 : firstOther + 1))
    setInsertSide('before')
    setInsertError('')
    insertDialog.current?.showModal()
  }

  function insertPages() {
    const target = Number(insertTarget)
    if (insertHasAnchor && (!Number.isInteger(target) || target < 1 || target > order.length)) {
      setInsertError(`请输入 1 到 ${order.length} 之间的成书位置`)
      return
    }
    const anchor = insertHasAnchor ? order[target - 1] : null
    if (anchor !== null && moving.includes(anchor)) {
      setInsertError('目标页也在本次移动中，请选择另一页作为插入位置')
      return
    }
    setLayout(insertPageBlock(layout, moving, anchor, insertSide))
    setIncluded(new Set([...included, ...moving]))
    chooseView('sequence')
    announce(`已插入 ${moving.length} 页`, moving.map((id) => `page:${id}`))
    insertDialog.current?.close()
  }

  function pagesForFile(id: string) {
    const tree = fileSubtree(id, fileOrder, parents)
    return layout.filter((page) => tree.has(pageById.get(page)!.source_id))
  }

  function moveFile(id: string, parent: string | null, target?: string, side: Side = 'after') {
    const subtree = fileSubtree(id, fileOrder, parents)
    if (parent && parents[parent]) { announce(CHILD_FILE_HELP); return }
    if ((parent && subtree.has(parent)) || (target && subtree.has(target))) return
    const next = moveFileTree(fileOrder, parents, id, parent, target, side)
    const movedPages = pagesForFile(id)
    const targetFiles = target ? fileSubtree(target, fileOrder, parents) : parent ? fileSubtree(parent, fileOrder, parents) : new Set<string>()
    const placement = findFilePageAnchor(order.map((page) => pageById.get(page)!), next.order, subtree, targetFiles, side)
    setFileOrder(next.order)
    setParents(next.parents)
    setLayout(insertPageBlock(layout, movedPages, placement.anchor, placement.side))
    announce('文件与页面顺序已同步更新', [`file:${id}`])
  }

  function changeEmbedFile(id: string) {
    setEmbedFile(id)
    setEmbedScope(pagesForFile(id).some((page) => included.has(page)) ? 'selected' : '')
    setEmbedError('')
    if (embedAnchor !== null && pagesForFile(id).includes(embedAnchor)) setEmbedAnchor(null)
  }

  function openFileInsert(parent: string, id?: string, anchor: number | null = null, side: Side = 'after') {
    if (parents[parent]) { announce(CHILD_FILE_HELP); return }
    const candidate = id ?? fileOrder.find((file) => file !== parent && !fileSubtree(file, fileOrder, parents).has(parent)) ?? ''
    setEmbedParent(parent)
    setEmbedFile(candidate)
    setEmbedScope(candidate && pagesForFile(candidate).some((page) => included.has(page)) ? 'selected' : '')
    setEmbedAnchor(anchor)
    setEmbedSide(side)
    setEmbedError('')
    fileDialog.current?.showModal()
  }

  function commitFileInsert(id: string, parent: string, anchor: number | null, side: Side, includeAll: boolean) {
    if (parents[parent]) { announce(CHILD_FILE_HELP); return }
    const subtree = fileSubtree(id, fileOrder, parents)
    if (subtree.has(parent)) return
    const movedPages = pagesForFile(id)
    const next = moveFileTree(fileOrder, parents, id, parent)
    const placement = anchor === null
      ? findFilePageAnchor(order.map((page) => pageById.get(page)!), next.order, subtree, fileSubtree(parent, fileOrder, parents), 'after')
      : { anchor, side }
    setFileOrder(next.order)
    setParents(next.parents)
    setLayout(insertPageBlock(layout, movedPages, placement.anchor, placement.side))
    if (includeAll) setIncluded(new Set([...included, ...movedPages]))
    chooseSource(parent)
    announce(`已将 ${fileById.get(id)!.filename} 嵌入 ${fileById.get(parent)!.filename}`, [`file:${id}`, ...movedPages.map((page) => `page:${page}`)])
  }

  function requestFileInsert(id: string, parent: string, anchor: number | null, side: Side) {
    if (parents[parent]) { announce(CHILD_FILE_HELP); return }
    if (fileSubtree(id, fileOrder, parents).has(parent)) return
    if (!pagesForFile(id).some((page) => included.has(page))) openFileInsert(parent, id, anchor, side)
    else commitFileInsert(id, parent, anchor, side, false)
  }

  function startDrag(event: DragEvent<HTMLElement>, item: Dragged) {
    dragging.current = item
    setDragged(item)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', `${item.kind}:${item.id}`)
  }

  function endDrag() {
    dragging.current = null
    setDragged(null)
    setDropTarget(null)
  }

  function dragClasses(key: string) {
    return `${dragged && `${dragged.kind}:${dragged.id}` === key ? ' is-dragging' : ''}${dropTarget?.key === key && !dropTarget.hideIndicator ? ` drop-${dropTarget.side}` : ''}${dropped.includes(key) ? ' is-dropped' : ''}`
  }

  function dragOverFile(event: DragEvent<HTMLElement>, id: string) {
    const item = dragging.current
    if (busy || item?.kind !== 'file' || fileSubtree(item.id, fileOrder, parents).has(id)) return
    const rect = event.currentTarget.getBoundingClientRect()
    const fraction = (event.clientY - rect.top) / rect.height
    const side = fraction < .25 ? 'before' : fraction > .75 ? 'after' : 'inside'
    if (side === 'inside' && parents[id]) {
      setDropTarget(null)
      setLiveMessage(CHILD_FILE_HELP)
      return
    }
    event.preventDefault()
    setDropTarget({ key: `file:${id}`, side })
  }

  function dropFile(event: DragEvent<HTMLElement>, id: string) {
    event.preventDefault()
    const item = dragging.current
    const side = dropTarget?.key === `file:${id}` ? dropTarget.side : null
    if (!busy && item?.kind === 'file' && side) {
      if (side === 'inside') requestFileInsert(item.id, id, null, 'after')
      else moveFile(item.id, parents[id], id, side)
    }
    endDrag()
  }

  function dragOverPage(event: DragEvent<HTMLElement>, id: number) {
    const item = dragging.current
    if (!item || busy || (item.kind === 'page' && item.id === id)) return
    if (item.kind === 'file' && parents[sourceId]) {
      setDropTarget(null)
      setLiveMessage(CHILD_FILE_HELP)
      return
    }
    if (item.kind === 'file' && (view !== 'source' || fileSubtree(item.id, fileOrder, parents).has(sourceId) || pagesForFile(item.id).includes(id))) return
    event.preventDefault()
    event.stopPropagation()
    const card = event.currentTarget
    const rect = card.getBoundingClientRect()
    const side = event.clientX < rect.left + rect.width / 2 ? 'before' : 'after'
    const neighbor = (side === 'before' ? card.previousElementSibling : card.nextElementSibling) as HTMLElement | null
    const hideIndicator = view === 'source' && (!neighbor || neighbor.offsetTop !== card.offsetTop)
    setDropTarget({ key: `page:${id}`, side, hideIndicator })
  }

  function dropPage(event: DragEvent<HTMLElement>, id: number) {
    event.preventDefault()
    event.stopPropagation()
    const item = dragging.current
    const side = dropTarget?.key === `page:${id}` ? dropTarget.side : null
    if (item && !busy && side && side !== 'inside') {
      if (item.kind === 'file' && view === 'source') requestFileInsert(item.id, sourceId, id, side)
      if (item.kind === 'page') {
        const from = visibleIds.indexOf(item.id)
        const index = visibleIds.indexOf(id) + (side === 'after' ? 1 : 0)
        reorderPage(from, index > from ? index - 1 : index)
      }
    }
    endDrag()
  }

  function openPreview(id: number) {
    setPreviewId(id)
    setZoomed(false)
    previewDialog.current?.showModal()
  }

  async function save() {
    setSaving(true)
    onBusyChange?.(true)
    try {
      const detail = await api.saveArrangement(bookId, { file_order: fileOrder, file_parents: parents, page_order: order })
      setSavedSignature(signature(fileOrder, parents, order))
      onDirtyChange?.(false)
      onSaved(detail)
      onNotice({ kind: 'success', text: `已保存 ${order.length} 页的编排。可在任务总览开始自动处理。` })
    } catch (error) {
      onNotice({ kind: 'error', text: error instanceof Error ? error.message : '编排保存失败，请重试' })
    } finally {
      setSaving(false)
      onBusyChange?.(false)
    }
  }

  if (loadError) return <div className="organizer-load-state" role="alert"><p>{loadError}</p>
    <button type="button" onClick={() => setLoadAttempt(loadAttempt + 1)}>重新读取</button></div>
  if (!arrangement) return <div className="organizer-load-state" role="status">正在读取项目文件和页面…</div>

  return <section ref={motionRef} className="organizer" aria-label="项目页面编排">
    <header className="organizer-heading">
      <div><h2>调整页面编排（可选）</h2><p>新上传页面默认全部勾选；保存后的页序用于下次任务。图片与 PDF 可以混排和嵌入。</p></div>
      <div className="organizer-count"><strong>{order.length}</strong><span>页已勾选 / 共 {arrangement.pages.length} 页</span></div>
    </header>
    <fieldset className="organizer-body" disabled={busy}>
      <legend className="organizer-sr-only">文件与页面编排</legend>
      <aside className="organizer-files" aria-label="文件顺序">
        <div className="organizer-section-title"><h3>文件顺序</h3><span>{fileOrder.length} 个文件</span></div>
        <ol className="organizer-file-list">
          {fileOrder.map((id, index) => {
            const file = fileById.get(id)!
            const parent = parents[id]
            const siblings = fileOrder.filter((file) => parents[file] === parent)
            const siblingIndex = siblings.indexOf(id)
            return <li key={id} data-motion-key={`file:${id}`} style={{ '--depth': fileDepth(id, parents) } as CSSProperties}
              className={`organizer-file organizer-tree-row${sourceId === id ? ' is-current' : ''}${parent ? ' is-child' : ''}${dragClasses(`file:${id}`)}`}
              draggable={!busy} onDragStart={(event) => startDrag(event, { kind: 'file', id })} onDragEnd={endDrag}
              onDragOver={(event) => dragOverFile(event, id)} onDrop={(event) => dropFile(event, id)}>
              <button type="button" className="organizer-file-main" onClick={() => chooseSource(id)} aria-current={sourceId === id ? 'true' : undefined}>
                <span className="organizer-file-number">{String(index + 1).padStart(2, '0')}</span>
                <span className="organizer-file-info"><strong title={file.filename}>{file.filename}</strong>
                  <span><span className={`organizer-kind ${file.kind}`}>{file.kind === 'pdf' ? 'PDF' : '图片'}</span> {file.page_count} 页 · 已勾选 {order.filter((page) => pageById.get(page)!.source_id === id).length} 页</span>
                  {parent && <span className="organizer-file-parent" title={fileById.get(parent)!.filename}>嵌入 {fileById.get(parent)!.filename}</span>}
                </span>
              </button>
              <div className="organizer-file-arrows">
                {parent && <button type="button" className="organizer-detach" disabled={busy} aria-label={`将 ${file.filename} 移至顶层`} onClick={() => moveFile(id, null)}>移出</button>}
                <button type="button" disabled={busy || siblingIndex === 0} aria-label={`上移文件 ${file.filename}`} onClick={() => moveFile(id, parent, siblings[siblingIndex - 1], 'before')}>↑</button>
                <button type="button" disabled={busy || siblingIndex === siblings.length - 1} aria-label={`下移文件 ${file.filename}`} onClick={() => moveFile(id, parent, siblings[siblingIndex + 1], 'after')}>↓</button>
              </div>
            </li>
          })}
        </ol>
        <div className={`organizer-file-root-drop${dropTarget?.key === 'root' ? ' drop-inside' : ''}`}
          onDragOver={(event) => {
            if (busy || dragging.current?.kind !== 'file') return
            event.preventDefault()
            setDropTarget({ key: 'root', side: 'inside' })
          }} onDrop={(event) => {
            event.preventDefault()
            if (!busy && dragging.current?.kind === 'file') moveFile(dragging.current.id, null)
            endDrag()
          }}>拖到这里，移至顶层末尾</div>
        <div className="organizer-file-tools">
          <p className="organizer-help">拖到文件上、下边缘调整顺序；拖到顶层文件中间嵌入。子文件保持同级，不再继续嵌套。</p>
          <p className="organizer-help">也可在右侧使用“插入文件…”，选择放入的位置。</p>
        </div>
      </aside>
      <div className="organizer-pages">
        <div className="organizer-view-tabs" role="group" aria-label="编排视图">
          <button type="button" aria-pressed={view === 'source'} onClick={() => chooseView('source')}>文件内选页与排序</button>
          <button type="button" aria-pressed={view === 'sequence'} onClick={() => chooseView('sequence')}>最终混合顺序 <span>{order.length}</span></button>
        </div>
        <div className="organizer-page-heading">
          <h3>{view === 'source' ? source?.filename : '成书顺序'}</h3>
          <p>{view === 'source'
            ? parents[sourceId] ? `拖动页面调整先后。${CHILD_FILE_HELP}` : '当前文件与嵌入文件的页面一起显示。拖动页面调整先后，或把左侧文件拖到页面之间。'
            : '这里显示下一次自动任务的默认页序。拖动页面，或使用“插入…”精确穿插。'}</p>
        </div>
        <div className="organizer-source-toolbar">
          {view === 'source' && source && <button type="button" disabled={busy || !!parents[sourceId] || !fileOrder.some((id) => id !== sourceId && !fileSubtree(id, fileOrder, parents).has(sourceId))} title={parents[sourceId] ? CHILD_FILE_HELP : undefined} onClick={() => openFileInsert(sourceId)}>插入文件…</button>}
          <span className="organizer-live-status" role="status" aria-live="polite">{liveMessage}</span>
        </div>
        {view === 'source' && source && <div className="organizer-range">
          <label htmlFor="organizer-range-scope">批量选页</label>
          <select id="organizer-range-scope" value={scope} onChange={(event) => { setScope(event.target.value as ProcessScope | 'all'); setRangeError('') }}>
            <option value="all">全部页</option><option value="odd">单数页</option><option value="even">偶数页</option><option value="custom">自定义范围</option>
          </select>
          {scope === 'custom' && <input aria-label="源文件页码范围" aria-invalid={!!rangeError} value={range} placeholder="如 1,3,8-12" onChange={(event) => { setRange(event.target.value); setRangeError('') }} />}
          <button type="button" onClick={selectRange}>应用范围</button>
          <span className="organizer-help">替换当前文件自身的勾选，源页码 1–{source.page_count}；其他文件及嵌入文件的勾选保持不变。</span>
          {rangeError && <p className="organizer-error" role="alert">{rangeError}</p>}
        </div>}
        <div className="organizer-batch">
          <label className="organizer-checkbox"><input type="checkbox" checked={displayedIds.length > 0 && displayedIds.every((id) => included.has(id))} disabled={busy || !displayedIds.length}
            onChange={(event) => {
              const next = new Set(included)
              displayedIds.forEach((id) => event.target.checked ? next.add(id) : next.delete(id))
              setIncluded(next)
            }} /><span>本屏</span></label>
          <span className="organizer-marked">当前视图已勾选 {selectedIds.length} 页</span>
          <button type="button" disabled={busy || !selectedIds.length} onClick={() => movePagesToEnd(selectedIds)}>移至末尾</button>
          <button type="button" disabled={busy || !selectedIds.length} onClick={() => openInsert(selectedIds)}>插入到…</button>
          {selectedIds.length > 0 && <button type="button" className="organizer-quiet" onClick={() => removePages(selectedIds)}>取消当前视图勾选</button>}
          <p className="organizer-batch-note organizer-help">勾选页加入本书编排。批量移动和取消勾选作用于当前视图，包含其他屏；取消后可在文件视图重新勾选。</p>
        </div>
        {!visibleIds.length ? <div className="organizer-empty"><h3>{view === 'sequence' ? '成书中暂无页面' : '当前文件暂无页面'}</h3>
          <p>{view === 'sequence' ? '返回文件视图，勾选页面即可重新加入，或使用页面的“插入…”指定位置。' : '在左侧选择有页面的文件继续编排。'}</p>
          {view === 'sequence' && <button type="button" onClick={() => chooseView('source')}>返回文件选页</button>}</div>
          : <div className={`organizer-page-grid ${view === 'sequence' ? 'is-sequence' : ''}`}>
            {displayedIds.map((id, index) => {
              const page = pageById.get(id)!
              const position = positions.get(id)
              const listIndex = currentPage * PAGE_SIZE + index
              return <article key={id} data-motion-key={`page:${id}`}
                className={`organizer-page-card${included.has(id) ? ' is-marked' : ''}${view === 'source' && page.source_id !== sourceId ? ' is-inserted' : ''}${dragClasses(`page:${id}`)}`}
                draggable={!busy} onDragStart={(event) => startDrag(event, { kind: 'page', id })} onDragEnd={endDrag}
                onDragOver={(event) => dragOverPage(event, id)} onDrop={(event) => dropPage(event, id)}>
                <div className="organizer-card-top"><label className="organizer-checkbox"><input type="checkbox" checked={included.has(id)} onChange={() => toggleIncluded(id)} aria-label={`将 ${page.source_filename} 源第 ${page.source_page} 页加入编排`} />
                  <span>{view === 'sequence' ? <><b>{String(listIndex + 1).padStart(2, '0')}</b> 成书页</> : `文件内第 ${listIndex + 1} 位`}</span></label></div>
                <div className="organizer-thumb"><PreviewImage key={id} bookId={bookId} page={page} />
                  <button type="button" className="organizer-preview-trigger" onClick={() => openPreview(id)} aria-label={`放大预览 ${page.source_filename} 源第 ${page.source_page} 页`}><span>放大预览</span></button>
                </div>
                <div className="organizer-card-caption"><strong title={page.source_filename}>{page.source_filename}</strong>
                  <span>源第 {page.source_page} 页 <span className={position ? 'organizer-included' : 'organizer-not-included'}>{position ? `已编入 · ${position}` : '未编入'}</span></span>
                </div>
                <div className="organizer-card-actions">
                  <button type="button" disabled={busy || listIndex === 0} aria-label={`前移 ${page.source_filename} 源第 ${page.source_page} 页`} onClick={() => reorderPage(listIndex, listIndex - 1)}>←</button>
                  <button type="button" disabled={busy || listIndex === visibleIds.length - 1} aria-label={`后移 ${page.source_filename} 源第 ${page.source_page} 页`} onClick={() => reorderPage(listIndex, listIndex + 1)}>→</button>
                  <button type="button" className="organizer-insert-button" onClick={() => openInsert([id])}>插入…</button>
                </div>
              </article>
            })}
          </div>}
        {view === 'source' && source && !parents[sourceId] && <div className={`organizer-page-end-drop${dropTarget?.key === 'page-end' ? ' drop-inside' : ''}`}
          onDragOver={(event) => {
            const item = dragging.current
            if (busy || item?.kind !== 'file' || fileSubtree(item.id, fileOrder, parents).has(sourceId)) return
            event.preventDefault()
            setDropTarget({ key: 'page-end', side: 'inside' })
          }} onDrop={(event) => {
            event.preventDefault()
            if (!busy && dragging.current?.kind === 'file') requestFileInsert(dragging.current.id, sourceId, null, 'after')
            endDrag()
          }}>将文件拖到这里，嵌入当前文件末尾</div>}
        {visibleIds.length > 0 && <nav className="organizer-pagination" aria-label="页面缩略图分页">
          <span>{currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, visibleIds.length)} / {visibleIds.length} 页</span>
          <div><button type="button" disabled={currentPage === 0} onClick={() => setPageIndex(currentPage - 1)}>上一屏</button>
            <label><span className="organizer-sr-only">跳转到第几屏</span><select value={currentPage} onChange={(event) => setPageIndex(Number(event.target.value))}>
              {Array.from({ length: lastPage + 1 }, (_, index) => <option key={index} value={index}>第 {index + 1} / {lastPage + 1} 屏</option>)}
            </select></label>
            <button type="button" disabled={currentPage === lastPage} onClick={() => setPageIndex(currentPage + 1)}>下一屏</button></div>
        </nav>}
      </div>
    </fieldset>
    <footer className="organizer-save-bar">
      <div><strong>{order.length} 页将按当前顺序保存</strong><span>{!order.length ? '请至少勾选一页后保存。' : dirty ? '保存调整后，回任务总览开始处理。' : '可保存并返回；开始任务时再一次确认处理范围与设置。'}</span></div>
      <button type="button" className="primary" disabled={busy || !order.length} onClick={() => void save()}>{saving ? '正在保存编排…' : '保存编排，回任务总览'}</button>
    </footer>
    <dialog ref={previewDialog} className="organizer-dialog organizer-preview-dialog" aria-labelledby="organizer-preview-title" onClick={(event) => { if (event.target === event.currentTarget) previewDialog.current?.close() }}>
      <div className="organizer-dialog-heading"><div><h3 id="organizer-preview-title">{previewPage?.source_filename}</h3><p>源第 {previewPage?.source_page} 页{previewId !== null && positions.has(previewId) ? ` · 成书第 ${positions.get(previewId)} 页` : ' · 未编入'}</p></div>
        <button type="button" onClick={() => previewDialog.current?.close()}>关闭预览</button></div>
      <div className={`organizer-preview-image ${zoomed ? 'is-zoomed' : ''}`}>
        {previewPage && <PreviewImage key={previewPage.number} bookId={bookId} page={previewPage} eager />}
      </div>
      <div className="organizer-preview-controls"><button type="button" disabled={previewPosition <= 0} onClick={() => setPreviewId(visibleIds[previewPosition - 1])}>上一页</button>
        <button type="button" aria-pressed={zoomed} onClick={() => setZoomed(!zoomed)}>{zoomed ? '适合窗口' : '查看原始大小'}</button>
        <button type="button" disabled={previewPosition < 0 || previewPosition >= visibleIds.length - 1} onClick={() => setPreviewId(visibleIds[previewPosition + 1])}>下一页</button></div>
    </dialog>
    <dialog ref={insertDialog} className="organizer-dialog organizer-insert-dialog" aria-labelledby="organizer-insert-title">
      <form onSubmit={(event) => { event.preventDefault(); insertPages() }}>
        <div className="organizer-dialog-heading"><div><h3 id="organizer-insert-title">插入 {moving.length} 页</h3><p>已编入的页面会移动位置，未编入的页面会加入。</p></div></div>
        <div className="organizer-insert-body">
          <p className="organizer-insert-source">{moving.length > 0 && `${pageById.get(moving[0])!.source_filename} · 源第 ${pageById.get(moving[0])!.source_page} 页`}{moving.length > 1 && ` 等 ${moving.length} 页，保持当前先后顺序`}</p>
          {insertHasAnchor ? <>
            <div className="organizer-insert-fields"><label htmlFor="organizer-insert-position">成书第</label><input id="organizer-insert-position" type="number" min="1" max={order.length} value={insertTarget} onChange={(event) => { setInsertTarget(event.target.value); setInsertError('') }} /><span>页</span>
              <select aria-label="插在目标页之前或之后" value={insertSide} onChange={(event) => setInsertSide(event.target.value as Side)}><option value="before">之前</option><option value="after">之后</option></select>
            </div>
            {anchorPage && <div className="organizer-insert-anchor"><div className="organizer-anchor-thumb"><PreviewImage key={anchorPage.number} bookId={bookId} page={anchorPage} /></div>
              <p><span>目标页面</span><strong>{anchorPage.source_filename}</strong>源第 {anchorPage.source_page} 页 · 成书第 {insertTarget} 页</p></div>}
          </> : <p>这些页面将按当前先后顺序放入成书。</p>}
          {insertError && <p className="organizer-error" role="alert">{insertError}</p>}
        </div>
        <div className="organizer-dialog-actions"><button type="button" onClick={() => insertDialog.current?.close()}>取消</button><button type="submit" className="primary" disabled={busy}>确认插入</button></div>
      </form>
    </dialog>
    <dialog ref={fileDialog} className="organizer-dialog organizer-insert-file-dialog" aria-labelledby="organizer-insert-file-title">
      <form onSubmit={(event) => {
        event.preventDefault()
        if (!embedFile || !embedScope) { setEmbedError('请选择要插入的文件及页面范围'); return }
        commitFileInsert(embedFile, embedParent, embedAnchor, embedSide, embedScope === 'all')
        fileDialog.current?.close()
      }}>
        <div className="organizer-dialog-heading"><div><h3 id="organizer-insert-file-title">插入文件</h3><p>嵌入 {fileById.get(embedParent)?.filename}，并在左侧显示为子文件。</p></div></div>
        <div className="organizer-insert-body">
          <label className="organizer-file-field" htmlFor="organizer-embed-file">选择项目文件
            <select id="organizer-embed-file" value={embedFile} onChange={(event) => changeEmbedFile(event.target.value)}>
              {embedCandidates.map((id) => <option key={id} value={id}>{fileById.get(id)!.filename}</option>)}
            </select>
          </label>
          <fieldset className="organizer-file-scope"><legend>编入哪些页面</legend>
            <label><input type="radio" name="embed-scope" value="selected" checked={embedScope === 'selected'} disabled={!embedSelectedCount} onChange={() => { setEmbedScope('selected'); setEmbedError('') }} />保持已编入的 {embedSelectedCount} 页</label>
            <label><input type="radio" name="embed-scope" value="all" checked={embedScope === 'all'} onChange={() => { setEmbedScope('all'); setEmbedError('') }} />编入此文件的全部 {embedPages.length} 页</label>
            <p className="organizer-help">{embedSelectedCount ? '包含已有子文件；保留各页当前的先后顺序。' : '这个文件还没有编入的页面。选择全部页面，或取消后先单独选页。'}{embedTree.size > 1 && '原有子文件会与此文件一起，成为目标文件的同级子文件。'}</p>
          </fieldset>
          <label className="organizer-file-field" htmlFor="organizer-embed-anchor">插入位置
            <select id="organizer-embed-anchor" value={embedAnchor ?? 'end'} onChange={(event) => setEmbedAnchor(event.target.value === 'end' ? null : Number(event.target.value))}>
              <option value="end">当前文件末尾</option>
              {embedTargets.map((id, index) => <option key={id} value={id}>{index + 1}. {pageById.get(id)!.source_filename} · 源第 {pageById.get(id)!.source_page} 页</option>)}
            </select>
          </label>
          {embedAnchor !== null && <div className="organizer-insert-fields"><label htmlFor="organizer-embed-side">插在目标页</label><select id="organizer-embed-side" value={embedSide} onChange={(event) => setEmbedSide(event.target.value as Side)}><option value="before">之前</option><option value="after">之后</option></select></div>}
          {embedAnchor !== null && pageById.has(embedAnchor) && <div className="organizer-insert-anchor"><div className="organizer-anchor-thumb"><PreviewImage key={embedAnchor} bookId={bookId} page={pageById.get(embedAnchor)!} /></div><p><span>目标页面</span><strong>{pageById.get(embedAnchor)!.source_filename}</strong>源第 {pageById.get(embedAnchor)!.source_page} 页</p></div>}
          {embedError && <p className="organizer-error" role="alert">{embedError}</p>}
        </div>
        <div className="organizer-dialog-actions"><button type="button" onClick={() => fileDialog.current?.close()}>取消</button><button type="submit" className="primary" disabled={busy || !embedFile}>插入文件</button></div>
      </form>
    </dialog>
  </section>
}
