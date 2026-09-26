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
  const updateField = (index: number, changes: Partial<CoverField>) => {
    onChange({ ...draft, cover_fields: draft.cover_fields.map((field, position) => position === index ? { ...field, ...changes } : field) })
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
      ? <label className="body-editor"><span>正文 Markdown 源文本</span><textarea aria-label="本页正文 Markdown 源文本" value={draft.text} onChange={(event) => onChange({ ...draft, text: event.target.value })} spellCheck={false} placeholder="本页暂无正文" /></label>
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
