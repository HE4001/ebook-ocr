import { useRef } from 'react'
import type { CoverField, PageDraft, PageKind } from './types'

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
        <label htmlFor="page-latex-body">LaTeX 正文</label>
        <div className="latex-tools" role="group" aria-label="插入 LaTeX 格式" onMouseDown={(event) => event.preventDefault()}>
          <button type="button" onClick={() => insert('\\begin{center}\n', '\n\\end{center}')}>居中</button>
          <button type="button" onClick={() => insert('\\begin{flushleft}\n', '\n\\end{flushleft}')}>左对齐</button>
          <button type="button" onClick={() => insert('\\begin{flushright}\n', '\n\\end{flushright}')}>右对齐</button>
          <button type="button" onClick={() => insert('\\noindent ')}>无缩进</button>
          <button type="button" onClick={() => insert('\\textbf{', '}')}>加粗</button>
          <button type="button" onClick={() => insert('{\\bfseries\\boldmath ', '}')}>文字与公式加粗</button>
          <button type="button" onClick={() => insert('\\textit{', '}')}>斜体</button>
          <button type="button" onClick={() => insert('\n\n')}>段落分隔</button>
          <button type="button" onClick={() => insert('\\[\n', '\n\\]')}>公式</button>
        </div>
        <textarea ref={bodyInput} id="page-latex-body" aria-label="本页 LaTeX 正文" value={draft.text} onChange={(event) => onChange({ ...draft, text: event.target.value })} spellCheck={false} placeholder="本页暂无正文" />
        <p className="latex-editor-hint">“加粗”用于纯文字；“文字与公式加粗”须选中完整的文字与公式区域。单独公式内的粗斜符号用 \boldsymbol，粗正体字母用 \mathbf；\mathscr、\mathbb 没有真实粗体，局部可用 \pmb 近似。使用空行分段；这里只编辑正文片段，页眉、页脚与封面由整书模板排版。</p>
      </div>
      : <div className="cover-editor">
        <p className="cover-editor-hint">只保留原页可见的书名、署名和出版信息。没有可辨认的信息时可留空。</p>
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
