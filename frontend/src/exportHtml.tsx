import { renderToStaticMarkup } from 'react-dom/server'
import { BookContent } from './Markdown'
import bookCss from './book.css?inline'
import type { BookDetail } from './types'

function escapeHtml(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;')
}

export function buildStandaloneHtml(detail: BookDetail): string {
  const title = escapeHtml(detail.book.title)
  const content = renderToStaticMarkup(<BookContent detail={detail} />)
  return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>' + title + '</title><style>' + bookCss + '</style></head>' +
    '<body>' + content + '</body></html>'
}

export function downloadText(filename: string, content: string, type: string): void {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function safeFilename(value: string): string {
  return value.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').trim() || '电子书'
}
