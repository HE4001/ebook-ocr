export function PdfPreview({ url, loading, error, warnings, title, emptyMessage }: {
  url: string | null
  loading: boolean
  error: string | null
  warnings: string[]
  title: string
  emptyMessage: string
}) {
  return <div className="pdf-preview" aria-busy={loading}>
    {error && <div className="pdf-error" role="alert"><strong>PDF 生成失败</strong><p>{error}</p></div>}
    {!loading && url && warnings.length > 0 && <div className="pdf-warnings" role="status">
      <strong>PDF 已生成，有排版提醒</strong>
      <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      <p>建议校对相关内容，调整长公式的分行或表格布局后重新生成 PDF。</p>
    </div>}
    {loading ? <p className="pdf-placeholder" role="status">正在编译 PDF…</p>
      : url ? <><iframe src={url} title={title} /><p className="pdf-viewer-help">无法在此显示时，可<a href={url} target="_blank" rel="noreferrer">在新窗口打开 PDF</a>。</p></>
        : !error && <p className="pdf-placeholder">{emptyMessage}</p>}
  </div>
}
