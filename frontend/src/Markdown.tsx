import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import type { BookDetail } from './types'

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

export function BookContent({ detail }: { detail: BookDetail }) {
  return (
    <article className="book-preview" id="print-manuscript">
      <h1 className="book-title">{detail.book.title}</h1>
      {detail.pages.map((page) => (
        <section className="book-page" key={page.number} data-page={page.number}>
          <div className="book-page-number">第 {page.number} 页</div>
          {page.text ? <Markdown text={page.text} /> : <p className="empty-page">本页暂无文本</p>}
        </section>
      ))}
    </article>
  )
}
