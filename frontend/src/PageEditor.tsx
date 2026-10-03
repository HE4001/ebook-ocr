import { useRef } from 'react'
import type { CoverField, PageDraft, PageKind, RenderStrategy } from './types'

const COVER_FIELD_LABELS: Record<CoverField['kind'], string> = {
  title: '书名',
  subtitle: '副标题',
  author: '作者',
  translator: '译者',
  editor: '编者',
  publisher: '出版社',
  series: '丛书',
  edition: '版次',
  publication_year: '出版年份',
  isbn: 'ISBN',
}

export function PageEditor({ draft, onChange, disabled }: {
  draft: PageDraft
  onChange: (draft: PageDraft) => void
  disabled: boolean
}) {
  const bodyInput = useRef<HTMLTextAreaElement>(null)
  const updateField = (index: number, changes: Partial<CoverField>) => {
    onChange({ ...draft, cover_fields: draft.cover_fields.map((field, position) => position === index ? { ...field, ...changes } : field) })
  }

  const insert = (prefix: string, suffix = '') => {
    const input = bodyInput.current!
    const start = input.selectionStart
    const end = input.selectionEnd
    const selected = draft.text.slice(start, end)
    onChange({ ...draft, text: draft.text.slice(0, start) + prefix + selected + suffix + draft.text.slice(end) })
    requestAnimationFrame(() => {
      input.focus()
      input.setSelectionRange(start + prefix.length, start + prefix.length + selected.length)
    })
  }

  return <fieldset className="page-editor" disabled={disabled}>
    <legend className="visually-hidden">本页校对</legend>
    <div className="source-authority-note"><label><span>本页渲染策略</span><select value={draft.render_strategy} onChange={(event) => onChange({ ...draft, render_strategy: event.target.value as RenderStrategy })}><option value="source_fidelity">原书还原 · 结构布局控制</option><option value="legacy_template">现有模板</option><option value="custom_latex">自定义源码 · 源码控制</option></select></label><p>{draft.render_strategy === 'source_fidelity' ? '以下是布局生成的源码。只查看或原样保存仍沿用布局；实际编辑源码后，将保存为自定义源码并脱离旧布局。' : draft.render_strategy === 'custom_latex' ? '版式由源码控制；已存原书布局保留，但不会覆盖自由源码。' : '使用现有模板。实际编辑源码后，保存为自定义源码；完整文档采用自身排版。'}</p></div>
    <div className="page-kind-control">
      <label htmlFor="page-kind">页面类型</label>
      <select id="page-kind" value={draft.page_kind} onChange={(event) => onChange({ ...draft, page_kind: event.target.value as PageKind })}>
        <option value="content">正文页</option>
        <option value="front_cover">封面</option>
        <option value="back_cover">封底</option>
      </select>
      <p>识别为封面或封底时自动应用对应版式，也可在此修正类型。切换时保留未保存草稿；保存后仅保留所选类型的内容。</p>
    </div>
    {draft.page_kind === 'content'
      ? <div className="body-editor">
        <label htmlFor="page-latex-body">LaTeX 源码</label>
        <div className="latex-tools" role="group" aria-label="插入 LaTeX 格式" onMouseDown={(event) => event.preventDefault()}>
          <button type="button" onClick={() => insert('\\begin{center}\n', '\n\\end{center}')}>居中</button>
          <button type="button" onClick={() => insert('\\begin{flushleft}\n', '\n\\end{flushleft}')}>左对齐</button>
          <button type="button" onClick={() => insert('\\begin{flushright}\n', '\n\\end{flushright}')}>右对齐</button>
          <button type="button" onClick={() => insert('\\noindent ')}>无缩进</button>
          <button type="button" onClick={() => insert('\\textbf{', '}')}>加粗</button>
          <button type="button" title="从文字模式选择包含完整公式定界符或环境的范围" onClick={() => insert('{\\bfseries\\boldmath ', '}')}>文字与公式加粗</button>
          <button type="button" title="在公式或上下标内选择数学内容，不包含公式定界符" onClick={() => insert('\\boldsymbol{', '}')}>公式内加粗</button>
          <button type="button" onClick={() => insert('\\textit{', '}')}>斜体</button>
          <button type="button" onClick={() => insert('\n\n')}>段落分隔</button>
          <button type="button" onClick={() => insert('\\[\n', '\n\\]')}>公式</button>
        </div>
        <textarea ref={bodyInput} id="page-latex-body" aria-label="本页 LaTeX 源码" value={draft.text} onChange={(event) => onChange({ ...draft, text: event.target.value })} spellCheck={false} placeholder="本页暂无源码" />
        <p className="latex-editor-hint">支持正文片段，也支持以 <code>{'\\documentclass'}</code> 开头的完整文档、宏包和自定义宏。片段使用项目模板；完整文档保留自身排版，不应用项目纸型、页眉页脚或装订设置。保留原行、行序和段落，超宽也不拆行、合行或缩放；语法、缺包和字体错误由 XeLaTeX 报告。</p>
        <p className="latex-editor-hint">黑体用 <code>{'{\\heiti 词语}'}</code>，粗体用 <code>{'\\textbf{词语}'}</code>，原页两者同时存在才叠加，不按语义自动加粗。“文字与公式加粗”只用于从文字模式选中、包含完整公式定界符或环境的范围；<code>{'\\bfseries'}</code> 和 <code>{'\\boldmath'}</code> 都须在数学模式外。</p>
        <p className="latex-editor-hint">公式内部及上下标使用“公式内加粗”，选中数学内容后插入 <code>{'\\boldsymbol{...}'}</code>，不选公式定界符；粗正体字母可用 <code>{'\\mathbf{A}'}</code>，不要将整式改为 <code>{'\\mathbf'}</code>。<code>{'\\textbf'}</code> 只改变文字字重；按钮和提示不会自动修复已有错误源码。</p>
      </div>
      : <div className="cover-editor">
        <p className="cover-editor-hint">只保留原页可见的书名、署名和出版信息，按原阅读顺序排列，并保留各项文字中的原有换行。没有可辨认的信息时可留空。</p>
        <div className="cover-field-list">
          {draft.cover_fields.map((field, index) => <div className="cover-field" key={index}>
            <label><span>类别</span><select aria-label={`第 ${index + 1} 项信息类别`} value={field.kind} onChange={(event) => updateField(index, { kind: event.target.value as CoverField['kind'] })}>
              {Object.entries(COVER_FIELD_LABELS).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
            </select></label>
            <label className="cover-field-text"><span>原页文字</span><textarea aria-label={`第 ${index + 1} 项${COVER_FIELD_LABELS[field.kind]}`} rows={2} value={field.text} onChange={(event) => updateField(index, { text: event.target.value })} /></label>
            <button type="button" className="cover-field-remove" aria-label={`删除第 ${index + 1} 项${COVER_FIELD_LABELS[field.kind]}`} onClick={() => onChange({ ...draft, cover_fields: draft.cover_fields.filter((_, position) => position !== index) })}>删除</button>
          </div>)}
        </div>
        <button type="button" onClick={() => onChange({ ...draft, cover_fields: [...draft.cover_fields, { kind: 'title', text: '' }] })}>＋ 添加书目信息</button>
      </div>}
  </fieldset>
}
