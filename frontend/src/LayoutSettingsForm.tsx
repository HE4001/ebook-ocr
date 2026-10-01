import { useEffect, useState } from 'react'
import { PAPER_SIZES } from './paper'
import type { LayoutSettings, PaperSize } from './types'

function draftFromLayout(layout: LayoutSettings) {
  return {
    font_family: layout.font_family,
    font_size_pt: layout.font_size_pt === null ? '' : String(layout.font_size_pt),
    line_height: String(layout.line_height),
    paragraph_indent: String(layout.paragraph_indent),
    paragraph_spacing_pt: String(layout.paragraph_spacing_pt),
    margin_mm: layout.margin_mm === null ? '' : String(layout.margin_mm),
  }
}

export function LayoutSettingsForm({ layout, paperSize, dirty, saving, disabled, onDirtyChange, onSave }: {
  layout: LayoutSettings
  paperSize: PaperSize
  dirty: boolean
  saving: boolean
  disabled: boolean
  onDirtyChange: (dirty: boolean) => void
  onSave: (layout: LayoutSettings) => Promise<boolean>
}) {
  const [draft, setDraft] = useState(() => draftFromLayout(layout))
  useEffect(() => { if (!dirty) setDraft(draftFromLayout(layout)) }, [layout, dirty])

  const change = <K extends keyof typeof draft>(key: K, value: (typeof draft)[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    onDirtyChange(true)
  }

  const save = async () => {
    const saved = await onSave({
      font_family: draft.font_family,
      font_size_pt: draft.font_size_pt === '' ? null : Number(draft.font_size_pt),
      line_height: Number(draft.line_height),
      paragraph_indent: Number(draft.paragraph_indent),
      paragraph_spacing_pt: Number(draft.paragraph_spacing_pt),
      margin_mm: draft.margin_mm === '' ? null : Number(draft.margin_mm),
    })
    if (saved) onDirtyChange(false)
  }

  return <form className="layout-settings" onSubmit={(event) => { event.preventDefault(); void save() }}>
    <fieldset disabled={disabled}>
      <legend>整书排版</legend>
      <div className="layout-fields">
        <label><span>字体</span><select value={draft.font_family} onChange={(event) => change('font_family', event.target.value as LayoutSettings['font_family'])}><option value="songti">宋体</option><option value="heiti">黑体</option><option value="kaiti">楷体</option></select></label>
        <label><span>字号（pt）</span><input type="number" min={6} max={48} step="any" value={draft.font_size_pt} placeholder={`纸型默认 ${PAPER_SIZES[paperSize].fontPt}`} onChange={(event) => change('font_size_pt', event.target.value)} /><small>留空使用纸型默认字号</small></label>
        <label><span>行距倍率</span><input type="number" min={1} max={3} step="any" required value={draft.line_height} onChange={(event) => change('line_height', event.target.value)} /></label>
        <label><span>首行缩进（汉字）</span><input type="number" min={0} max={8} step="any" required value={draft.paragraph_indent} onChange={(event) => change('paragraph_indent', event.target.value)} /></label>
        <label><span>段距（pt）</span><input type="number" min={0} max={48} step="any" required value={draft.paragraph_spacing_pt} onChange={(event) => change('paragraph_spacing_pt', event.target.value)} /></label>
        <label><span>页边距（mm）</span><input type="number" min={2} max={50} step="any" value={draft.margin_mm} placeholder={`纸型默认 ${PAPER_SIZES[paperSize].marginMm}`} onChange={(event) => change('margin_mm', event.target.value)} /><small>留空使用纸型默认边距</small></label>
      </div>
      <div className="layout-save-row"><p>{dirty ? '排版设置尚未保存；保存后更新 PDF。' : '以上设置已保存，应用于整书和本页 PDF。'}</p><button className="primary" type="submit" disabled={!dirty}>{saving ? '保存中…' : '保存排版'}</button><button type="button" disabled={!dirty} onClick={() => { setDraft(draftFromLayout(layout)); onDirtyChange(false) }}>恢复已保存设置</button></div>
    </fieldset>
  </form>
}
