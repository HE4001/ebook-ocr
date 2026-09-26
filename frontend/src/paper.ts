import type { CSSProperties } from 'react'
import type { PaperSize } from './types'

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

export function paperStyle(size: PaperSize = 'a4'): CSSProperties {
  const paper = PAPER_SIZES[size]
  return {
    '--paper-width': `${paper.widthMm}mm`,
    '--paper-width-mm': paper.widthMm,
    '--paper-height-mm': paper.heightMm,
    '--paper-margin-mm': paper.marginMm,
    '--paper-margin-shift-mm': paper.marginMm * 0.2,
    '--paper-font-mm': paper.fontPt * 25.4 / 72,
  } as CSSProperties
}

export function paperPrintCss(size: PaperSize = 'a4', printVersion = false): string {
  const paper = PAPER_SIZES[size]
  const base = `@page { size: ${paper.widthMm}mm ${paper.heightMm}mm; margin: ${paper.marginMm}mm; }`
  if (!printVersion) return base
  const shift = paper.marginMm * 0.2
  const inner = paper.marginMm + shift
  const outer = paper.marginMm - shift
  return `${base}
    @page book-centered { margin: ${paper.marginMm}mm; }
    @page book-left { margin-left: ${outer}mm; margin-right: ${inner}mm; }
    @page book-right { margin-left: ${inner}mm; margin-right: ${outer}mm; }`
}
