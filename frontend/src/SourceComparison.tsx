import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api } from './api'
import { outputBoxToNormalized, outputPointToSource, sourceBoxToOutput, sourcePointToOutput } from './proofingGeometry'
import type { PagePoint } from './proofingGeometry'
import type { BBox, BoxBp, Page, PdfCompileResult } from './types'

export type ProofingFocus = { token: number; book_id: string; page_number: number; content_revision: number; layout_revision: number; source_bbox: BBox | null; output_bbox_bp: BoxBp | null; output_page: number | null; line_id: string | null }
type Size = { width: number; height: number }
type Side = 'source' | 'output'

function highlightStyle(box: BBox) {
  return { left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }
}

export function SourceComparison({ bookId, page, result, loading, focus }: {
  bookId: string; page: Page; result: PdfCompileResult | null; loading: boolean; focus: ProofingFocus | null
}) {
  const mapping = result?.page_map.find((entry) => entry.page_number === page.number)
  const start = mapping?.output_page_start ?? 1
  const end = mapping?.output_page_end ?? 1
  const [outputPage, setOutputPage] = useState(start)
  const [side, setSide] = useState<Side>('source')
  const [fit, setFit] = useState<'page' | 'width'>('page')
  const [zoom, setZoom] = useState(1)
  const [imageError, setImageError] = useState(false)
  const [sourceSize, setSourceSize] = useState<Size>({ width: page.source_metadata?.canonical_width_px ?? 1, height: page.source_metadata?.canonical_height_px ?? 1 })
  const [outputSize, setOutputSize] = useState<Size>({ width: 1, height: 1 })
  const [viewSizes, setViewSizes] = useState<Record<Side, Size>>({ source: { width: 1, height: 1 }, output: { width: 1, height: 1 } })
  const comparisonRegion = useRef<HTMLElement>(null)
  const sourceViewport = useRef<HTMLDivElement>(null)
  const outputViewport = useRef<HTMLDivElement>(null)
  const sourceImage = useRef<HTMLDivElement>(null)
  const outputImage = useRef<HTMLDivElement>(null)
  const positions = useRef<Record<Side, PagePoint>>({ source: { x: 0.5, y: 0.5 }, output: { x: 0.5, y: 0.5 } })
  const syncing = useRef(false)
  const releaseFrame = useRef(0)
  const registered = Boolean(mapping?.source_to_output_affine && mapping.output_width_bp && mapping.output_height_bp && start === end)
  const outputUrl = result?.pdf_url ? api.compiledPageUrl(result.pdf_url, outputPage) : null

  useEffect(() => { setOutputPage(start); setImageError(false) }, [result?.pdf_url, start])
  useEffect(() => { setImageError(false) }, [outputUrl])
  useEffect(() => {
    if (focus) comparisonRegion.current!.scrollIntoView({ block: 'start', inline: 'nearest' })
  }, [focus])
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      setViewSizes((current) => {
        const source = sourceViewport.current!
        const output = outputViewport.current!
        return {
          source: source.clientWidth ? { width: source.clientWidth, height: source.clientHeight }
            : output.clientWidth ? { width: output.clientWidth, height: output.clientHeight } : current.source,
          output: output.clientWidth ? { width: output.clientWidth, height: output.clientHeight }
            : source.clientWidth ? { width: source.clientWidth, height: source.clientHeight } : current.output,
        }
      })
    })
    observer.observe(sourceViewport.current!)
    observer.observe(outputViewport.current!)
    return () => { observer.disconnect(); cancelAnimationFrame(releaseFrame.current) }
  }, [])

  const [a, b] = mapping?.source_to_output_affine ?? [1, 0]
  const outputRatio = registered ? sourceSize.width / Math.hypot(a, b) : sourceSize.width / (mapping?.output_width_bp ?? outputSize.width)
  const outputLogical = {
    width: (mapping?.output_width_bp ?? outputSize.width) * outputRatio,
    height: (mapping?.output_height_bp ?? outputSize.height) * outputRatio,
  }
  const fitScale = (viewport: Size, image: Size) => fit === 'width' ? (viewport.width - 24) / image.width
    : Math.min((viewport.width - 24) / image.width, (viewport.height - 24) / image.height)
  const scale = Math.max(0.001, Math.min(fitScale(viewSizes.source, sourceSize), ...(outputUrl ? [fitScale(viewSizes.output, outputLogical)] : []))) * zoom

  const place = (target: Side, point: PagePoint) => {
    const viewport = target === 'source' ? sourceViewport.current! : outputViewport.current!
    const image = target === 'source' ? sourceImage.current : outputImage.current
    if (!image || !viewport.clientWidth || !image.clientWidth) return
    const viewRect = viewport.getBoundingClientRect()
    const imageRect = image.getBoundingClientRect()
    viewport.scrollLeft = viewport.scrollLeft + imageRect.left - viewRect.left + point.x * imageRect.width - viewport.clientWidth / 2
    viewport.scrollTop = viewport.scrollTop + imageRect.top - viewRect.top + point.y * imageRect.height - viewport.clientHeight / 2
  }
  const endSync = () => {
    cancelAnimationFrame(releaseFrame.current)
    releaseFrame.current = requestAnimationFrame(() => { syncing.current = false })
  }

  useLayoutEffect(() => {
    syncing.current = true
    place('source', positions.current.source)
    place('output', positions.current.output)
    endSync()
  }, [scale, side, outputUrl, viewSizes])

  useEffect(() => {
    if (!focus) return
    if (focus.output_page != null && focus.output_page >= start && focus.output_page <= end) setOutputPage(focus.output_page)
    const box = focus.source_bbox
    const point = box ? { x: (box[0] + box[2]) / 2, y: (box[1] + box[3]) / 2 } : { x: 0.5, y: 0.1 }
    positions.current.source = point
    if (registered) positions.current.output = sourcePointToOutput(point, mapping!)
    else if (focus.output_bbox_bp && mapping?.output_width_bp && mapping.output_height_bp) {
      const outputBox = outputBoxToNormalized(focus.output_bbox_bp, mapping)
      positions.current.output = { x: (outputBox[0] + outputBox[2]) / 2, y: (outputBox[1] + outputBox[3]) / 2 }
    }
    syncing.current = true
    place('source', positions.current.source)
    place('output', positions.current.output)
    endSync()
    setSide('source')
  }, [focus, registered, mapping, start, end])

  const scroll = (scrolledSide: Side) => {
    if (syncing.current) return
    const viewport = scrolledSide === 'source' ? sourceViewport.current! : outputViewport.current!
    const image = scrolledSide === 'source' ? sourceImage.current : outputImage.current
    if (!image) return
    const viewRect = viewport.getBoundingClientRect()
    const imageRect = image.getBoundingClientRect()
    const point = { x: (viewRect.left + viewport.clientWidth / 2 - imageRect.left) / imageRect.width,
      y: (viewRect.top + viewport.clientHeight / 2 - imageRect.top) / imageRect.height }
    positions.current[scrolledSide] = point
    if (!registered) return
    const target = scrolledSide === 'source' ? 'output' : 'source'
    const transformed = scrolledSide === 'source' ? sourcePointToOutput(point, mapping!) : outputPointToSource(point, mapping!)
    positions.current[target] = transformed
    syncing.current = true
    place(target, transformed)
    endSync()
  }

  const sourceHighlight = focus?.source_bbox
  const outputHighlight = focus?.output_bbox_bp && mapping?.output_width_bp && mapping.output_height_bp
    ? outputBoxToNormalized(focus.output_bbox_bp, mapping)
    : sourceHighlight && registered ? sourceBoxToOutput(sourceHighlight, mapping!) : null

  return <section ref={comparisonRegion} className={`source-comparison showing-${side}`} aria-label="原图与输出同页对照">
    <div className="comparison-toolbar">
      <div className="comparison-side-switch" role="group" aria-label="窄窗显示页面"><button aria-pressed={side === 'source'} onClick={() => setSide('source')}>原图</button><button aria-pressed={side === 'output'} onClick={() => setSide('output')}>输出</button></div>
      <div className="comparison-zoom" role="group" aria-label="同步缩放"><button aria-pressed={fit === 'page' && zoom === 1} onClick={() => { setFit('page'); setZoom(1) }}>适合整页</button><button aria-pressed={fit === 'width' && zoom === 1} onClick={() => { setFit('width'); setZoom(1) }}>适合宽度</button><button aria-label="缩小对照" disabled={zoom <= 0.25} onClick={() => setZoom((value) => Math.max(0.25, value / 1.25))}>−</button><span>{Math.round(zoom * 100)}%</span><button aria-label="放大对照" disabled={zoom >= 6} onClick={() => setZoom((value) => Math.min(6, value * 1.25))}>＋</button></div>
      {mapping && <label className="output-page-picker">输出页 <select value={outputPage} onChange={(event) => setOutputPage(Number(event.target.value))}>{Array.from({ length: end - start + 1 }, (_, index) => start + index).map((number) => <option key={number} value={number}>{number}</option>)}</select></label>}
    </div>
    <p className="comparison-note">{registered ? '两侧按源页坐标同步位置与缩放。' : '源页与输出尚无单页配准映射，位置独立保留；两侧可同步缩放。'}{mapping && ` 源文件第 ${page.source_page} 页 → 输出第 ${start}${end > start ? `—${end}` : ''} 页。`}</p>
    <div className="comparison-grid">
      <section className="comparison-pane source-pane"><h3>原图 · {page.source_filename} 第 {page.source_page} 页</h3>
        <div className="comparison-viewport" ref={sourceViewport} onScroll={() => scroll('source')} tabIndex={0} aria-label="原图页内浏览">
          <div className="comparison-stage"><div ref={sourceImage} className="comparison-image" style={{ width: sourceSize.width * scale, height: sourceSize.height * scale }}>
            <img src={api.pagePreviewUrl(bookId, page.number)} alt={`${page.source_filename}第${page.source_page}页原图`} draggable={false} onLoad={(event) => setSourceSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} />
            {sourceHighlight && <span className="comparison-highlight" style={highlightStyle(sourceHighlight)} aria-label={`原图诊断区域${focus?.line_id ? ` ${focus.line_id}` : ''}`} />}
          </div></div>
        </div>
      </section>
      <section className="comparison-pane output-pane"><h3>输出{mapping ? ` · 第 ${outputPage} 页` : ''}</h3>
        <div className="comparison-viewport" ref={outputViewport} onScroll={() => scroll('output')} tabIndex={0} aria-label="输出页内浏览">
          {loading ? <p className="comparison-placeholder" role="status">正在编译本页…</p>
            : outputUrl ? imageError ? <p className="comparison-placeholder" role="alert">输出页图像读取失败。可在下方查看或下载完整 PDF。</p>
              : <div className="comparison-stage"><div ref={outputImage} className="comparison-image" style={{ width: outputLogical.width * scale, height: outputLogical.height * scale }}>
                <img src={outputUrl} alt={`PDF 输出第${outputPage}页`} draggable={false} onLoad={(event) => setOutputSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} onError={() => setImageError(true)} />
                {outputHighlight && (focus?.output_page == null || focus.output_page === outputPage) && <span className="comparison-highlight" style={highlightStyle(outputHighlight)} aria-label="输出诊断区域" />}
              </div></div>
              : <p className="comparison-placeholder">预览本页草稿后，在此对照原图和输出。</p>}
        </div>
      </section>
    </div>
  </section>
}
