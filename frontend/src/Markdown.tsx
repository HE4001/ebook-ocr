import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import type { BookDetail, MarginSegment, Page } from './types'

function safeUrl(url: string): string {
  const value = url.trim()
  return /^(https?:\/\/|mailto:|#)/i.test(value) ? value : ''
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown-body">
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
  const rows = [...new Set(segments.map((segment) => segment.row))].sort((a, b) => a - b)

  return <div className={`page-margin page-margin-${placement}`} aria-label={placement === 'header' ? '原书页眉' : '原书页脚'}>
    {rows.map((row, index) => <div className="margin-row" key={row} style={{ marginTop: `${(index === 0 ? row - 1 : row - rows[index - 1] - 1) * .25}em` }}>
      {alignments.map((alignment) => <div className={`margin-slot margin-slot-${alignment}`} key={alignment}>
        {segments.filter((segment) => segment.row === row && segment.alignment === alignment).map((segment, index) =>
          <span key={index} className={`margin-segment margin-size-${segment.font_size}${segment.bold ? ' margin-bold' : ''}${segment.italic ? ' margin-italic' : ''}${segment.kind === 'page_number' ? ' source-page-number' : ''}`}>{segment.text}</span>)}
      </div>)}
    </div>)}
  </div>
}

export function PageContent({ page, text = page.text }: { page: Page; text?: string }) {
  return <div className="book-page-entry">
    <div className="book-page-number">源文件第 {page.number} 页</div>
    <section className="book-page" data-page={page.number} aria-label={`源文件第 ${page.number} 页排版`}>
      <MarginContent segments={page.header_segments} placement="header" />
      <div className="page-body">{text && <Markdown text={text} />}</div>
      <MarginContent segments={page.footer_segments} placement="footer" />
    </section>
  </div>
}

export function BookContent({ detail }: { detail: BookDetail }) {
  return <article className="book-preview" id="print-manuscript">
    <h1 className="book-title">{detail.book.title}</h1>
    {detail.pages.map((page) => <PageContent key={page.number} page={page} />)}
  </article>
}
