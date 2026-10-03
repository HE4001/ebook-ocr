import type { LayoutSettings, Page, PageSide, PaperSize } from './types'
import { isLatexDocument } from './latexDocument'

export function bindingPageSide(page: Pick<Page, 'page_kind' | 'page_side' | 'footer_segments' | 'render_strategy' | 'text'>,
  fidelityPaper: LayoutSettings['source_fidelity_paper'] = 'project'): PageSide {
  if (page.page_kind !== 'content' || (page.page_side !== 'left' && page.page_side !== 'right')) return 'unknown'
  if (page.render_strategy === 'source_fidelity') return fidelityPaper === 'project' ? page.page_side : 'unknown'
  if (isLatexDocument(page.text)) return 'unknown'
  return page.footer_segments.some((segment) => segment.text.trim()) ? page.page_side : 'unknown'
}

export const PAPER_SIZES: Record<PaperSize, {
  label: string
  widthMm: number
  heightMm: number
  marginMm: number
  fontPt: number
}> = {
  a4: { label: 'A4 · 210 × 297 mm', widthMm: 210, heightMm: 297, marginMm: 18, fontPt: 12 },
  a5: { label: 'A5 · 148 × 210 mm', widthMm: 148, heightMm: 210, marginMm: 14, fontPt: 11 },
  a6: { label: 'A6 · 105 × 148 mm', widthMm: 105, heightMm: 148, marginMm: 10, fontPt: 10.5 },
  b5: { label: 'B5（ISO）· 176 × 250 mm', widthMm: 176, heightMm: 250, marginMm: 16, fontPt: 11.5 },
  b6: { label: 'B6（ISO）· 125 × 176 mm', widthMm: 125, heightMm: 176, marginMm: 12, fontPt: 11 },
  trade_6x9: { label: '6 × 9 英寸 · 152.4 × 228.6 mm', widthMm: 152.4, heightMm: 228.6, marginMm: 14, fontPt: 11 },
}
