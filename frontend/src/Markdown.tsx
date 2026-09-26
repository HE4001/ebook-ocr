import { useLayoutEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import { bindingPageSide, paperPrintCss, paperStyle } from './paper'
import { installDisplayMathFit } from './fitDisplayMath'
import type { BookDetail, CoverField, MarginSegment, Page, PaperSize } from './types'

function safeUrl(url: string): string {
  const value = url.trim()
  return /^(https?:\/\/|mailto:|#)/i.test(value) ? value : ''
}

export function Markdown({ text, paperSize = 'a4' }: { text: string; paperSize?: PaperSize }) {
  const root = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => installDisplayMathFit(root.current!), [text, paperSize])
  return (
    <div ref={root} className="markdown-body">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { output: 'mathml', trust: false }]]}
        urlTransform={safeUrl}
        components={{
          img: ({ alt }) => <span className="omitted-image">{alt || '[图片链接已省略]'}</span>,
          a: ({ href, children }) => href ? <a href={href} rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
        }}
      >{text}</ReactMarkdown>
    </div>
  )
}

const alignments = ['left', 'center', 'right'] as const

export function MarginContent({ segments, placement }: {
  segments: MarginSegment[]
  placement: 'header' | 'footer'
}) {
  if (!segments.length) return null
  const rows = Array.from({ length: Math.max(...segments.map((segment) => segment.row)) }, (_, index) => index + 1)

  return <div className={`page-margin page-margin-${placement}`} aria-label={placement === 'header' ? '原书页眉' : '原书页脚'}>
    {rows.map((row) => {
      const rowSegments = segments.filter((segment) => segment.row === row)
      const occupied = alignments.filter((alignment) => rowSegments.some((segment) => segment.alignment === alignment))
      const layout = occupied.length < 2 ? 'single' : occupied.includes('center') ? 'centered' : 'edges'
      return <div className={`margin-row margin-row-${layout}`} key={row} data-row={row}>
        {occupied.map((alignment) => <div className={`margin-slot margin-slot-${alignment}`} key={alignment}>
          {rowSegments.filter((segment) => segment.alignment === alignment).map((segment, index) =>
            <span key={index} className={`margin-segment margin-size-${segment.font_size}${segment.bold ? ' margin-bold' : ''}${segment.italic ? ' margin-italic' : ''}${segment.kind === 'page_number' ? ' source-page-number' : ''}`}>{segment.text}</span>)}
        </div>)}
      </div>
    })}
  </div>
}

function CoverContent({ fields }: { fields: CoverField[] }) {
  const groups: { name: string; kinds: CoverField['kind'][] }[] = [
    { name: 'heading', kinds: ['series', 'title', 'subtitle'] },
    { name: 'credits', kinds: ['author', 'translator', 'editor'] },
    { name: 'publication', kinds: ['publisher', 'edition', 'publication_year', 'isbn'] },
  ]

  return <div className="cover-content">
    {groups.map(({ name, kinds }) => {
      const entries = kinds.flatMap((kind) => fields.filter((field) => field.kind === kind && field.text.trim()))
      return entries.length > 0 && <div className={`cover-${name}`} key={name}>
        {entries.map((field, index) => <p className={`cover-field-${field.kind}`} key={index}>{field.text}</p>)}
      </div>
    })}
  </div>
}

export function PageContent({ page, text = page.text, paperSize = 'a4' }: { page: Page; text?: string; paperSize?: PaperSize }) {
  const pageSide = bindingPageSide(page)
  return <div className="book-page-entry" style={paperStyle(paperSize)} data-page-side={pageSide}>
    <div className="book-page-number">{page.source_filename} · 第 {page.source_page} 页</div>
    <section className={`book-page book-page-${page.page_kind}`} data-page={page.number} aria-label={`${page.source_filename} 第 ${page.source_page} 页排版`}>
      {page.page_kind === 'content' ? <>
        <MarginContent segments={page.header_segments} placement="header" />
        <div className="page-body">{text && <Markdown text={text} paperSize={paperSize} />}</div>
        <MarginContent segments={page.footer_segments} placement="footer" />
      </> : <CoverContent fields={page.cover_fields} />}
    </section>
  </div>
}

export function BookContent({ detail, printVersion = false }: { detail: BookDetail; printVersion?: boolean }) {
  return <article className={`book-preview${printVersion ? ' book-preview-print' : ''}`} id="print-manuscript" style={paperStyle(detail.book.paper_size)}>
    <style>{paperPrintCss(detail.book.paper_size, printVersion)}</style>
    <h1 className="book-title">{detail.book.title}</h1>
    {detail.pages.map((page) => <PageContent key={page.number} page={page} paperSize={detail.book.paper_size} />)}
  </article>
}
