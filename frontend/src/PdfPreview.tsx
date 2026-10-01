export function PdfPreview({ url, loading, error, title, emptyMessage }: {
  url: string | null
  loading: boolean
  error: string | null
  title: string
  emptyMessage: string
}) {
  return <div className="pdf-preview" aria-busy={loading}>
    {error && <div className="pdf-error" role="alert"><strong>PDF 生成失败</strong><p>{error}</p></div>}
    {loading ? <p className="pdf-placeholder" role="status">正在编译 PDF…</p>
      : url ? <><iframe src={url} title={title} /><p className="pdf-viewer-help">无法在此显示时，可<a href={url} target="_blank" rel="noreferrer">在新窗口打开 PDF</a>。</p></>
        : !error && <p className="pdf-placeholder">{emptyMessage}</p>}
  </div>
}
