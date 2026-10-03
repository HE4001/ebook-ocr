import { useEffect, useState } from 'react'
import { PAPER_SIZES } from './paper'
import type { LayoutSettings, PaperSize, RenderStrategy } from './types'

function draftFromLayout(layout: LayoutSettings) {
  return {
    source_fidelity_paper: layout.source_fidelity_paper,
    font_family: layout.font_family,
    font_size_pt: layout.font_size_pt === null ? '' : String(layout.font_size_pt),
    line_height: String(layout.line_height),
    paragraph_indent: String(layout.paragraph_indent),
    paragraph_spacing_pt: String(layout.paragraph_spacing_pt),
    margin_mm: layout.margin_mm === null ? '' : String(layout.margin_mm),
  }
}

export function LayoutSettingsForm({ layout, paperSize, renderStrategy, dirty, saving, disabled, onDirtyChange, onSave }: {
  layout: LayoutSettings
  paperSize: PaperSize
  renderStrategy: RenderStrategy
  dirty: boolean
  saving: boolean
  disabled: boolean
  onDirtyChange: (dirty: boolean) => void
  onSave: (layout: LayoutSettings, renderStrategy: RenderStrategy) => Promise<boolean>
}) {
  const [draft, setDraft] = useState(() => draftFromLayout(layout))
  const [strategy, setStrategy] = useState(renderStrategy)
  useEffect(() => { if (!dirty) setDraft(draftFromLayout(layout)) }, [layout, dirty])
  useEffect(() => { if (!dirty) setStrategy(renderStrategy) }, [renderStrategy, dirty])

  const change = <K extends keyof typeof draft>(key: K, value: (typeof draft)[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    onDirtyChange(true)
  }

  const save = async () => {
    const saved = await onSave({
      source_fidelity_paper: draft.source_fidelity_paper,
      font_family: draft.font_family,
      font_size_pt: draft.font_size_pt === '' ? null : Number(draft.font_size_pt),
      line_height: Number(draft.line_height),
      paragraph_indent: Number(draft.paragraph_indent),
      paragraph_spacing_pt: Number(draft.paragraph_spacing_pt),
      margin_mm: draft.margin_mm === '' ? null : Number(draft.margin_mm),
    }, strategy)
    if (saved) onDirtyChange(false)
  }

  return <form className="layout-settings" onSubmit={(event) => { event.preventDefault(); void save() }}>
    <fieldset disabled={disabled}>
      <legend>整书排版</legend>
      <label className="book-strategy"><span>项目渲染策略</span><select value={strategy} onChange={(event) => { setStrategy(event.target.value as RenderStrategy); onDirtyChange(true) }}><option value="source_fidelity">原书还原</option><option value="legacy_template">现有模板</option><option value="custom_latex">自定义源码</option></select><small>保存策略会更新仍沿用项目策略的页面；单独设置的页面继续使用自身策略。旧书保留现有模板，新书缺少布局时需校准。</small></label>
      <label className="fidelity-paper"><span>原书还原纸张</span><select value={draft.source_fidelity_paper} onChange={(event) => change('source_fidelity_paper', event.target.value as LayoutSettings['source_fidelity_paper'])}><option value="project">项目纸型 · 原页整体等比映射</option><option value="source">源页尺寸 · 保留原页画布</option></select><small>只影响原书还原页。实际纸张和比例以生成后的页映射为准。</small></label>
      <p className="layout-scope-note">{strategy === 'source_fidelity' ? '原书还原使用各页版心与基线，模板行距、缩进和段距不会覆盖原行。未校准字体暂用项目字体预览；项目纸型的边距决定整页映射留白。' : strategy === 'custom_latex' ? '版式由源码控制；完整文档采用自身纸张、字体和排版。模板设置应用于模板页及源码片段的外层模板。' : '模板字体、行距、缩进与段距应用于现有模板页；完整自定义文档由源码控制。'}</p>
      <div className="layout-fields">
        <label><span>模板 / 未校准预览字体</span><select value={draft.font_family} onChange={(event) => change('font_family', event.target.value as LayoutSettings['font_family'])}><option value="songti">宋体</option><option value="heiti">黑体</option><option value="kaiti">楷体</option></select></label>
        <label><span>模板 / 未校准预览字号（pt）</span><input type="number" min={6} max={48} step="any" value={draft.font_size_pt} placeholder={`纸型默认 ${PAPER_SIZES[paperSize].fontPt}`} onChange={(event) => change('font_size_pt', event.target.value)} /><small>留空使用纸型默认字号；已校准字号优先</small></label>
        <label><span>模板行距倍率</span><input type="number" min={1} max={3} step="any" required value={draft.line_height} onChange={(event) => change('line_height', event.target.value)} /></label>
        <label><span>模板首行缩进（汉字）</span><input type="number" min={0} max={8} step="any" required value={draft.paragraph_indent} onChange={(event) => change('paragraph_indent', event.target.value)} /></label>
        <label><span>模板段距（pt）</span><input type="number" min={0} max={48} step="any" required value={draft.paragraph_spacing_pt} onChange={(event) => change('paragraph_spacing_pt', event.target.value)} /></label>
        <label><span>项目纸型留白（mm）</span><input type="number" min={2} max={50} step="any" value={draft.margin_mm} placeholder={`纸型默认 ${PAPER_SIZES[paperSize].marginMm}`} onChange={(event) => change('margin_mm', event.target.value)} /><small>模板边距或原书还原整页留白；源页尺寸模式忽略</small></label>
      </div>
      <div className="layout-save-row"><p>{dirty ? '排版设置尚未保存；保存后更新 PDF。' : '设置已保存，各页按自己的渲染策略应用。'}</p><button className="primary" type="submit" disabled={!dirty}>{saving ? '保存中…' : '保存排版'}</button><button type="button" disabled={!dirty} onClick={() => { setDraft(draftFromLayout(layout)); setStrategy(renderStrategy); onDirtyChange(false) }}>恢复已保存设置</button></div>
    </fieldset>
  </form>
}
