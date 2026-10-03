import type { PdfCompileResult, QualityStatus, RenderDiagnostic } from './types'

const QUALITY_LABEL: Record<QualityStatus, string> = {
  passed: '已检测项目通过', needs_review: '需要检查', unverified: '尚未验证', compile_failed: '编译失败',
}

export function PdfPreview({ result, loading, error, title, emptyMessage, onDiagnostic }: {
  result: PdfCompileResult | null
  loading: boolean
  error: string | null
  title: string
  emptyMessage: string
  onDiagnostic: (diagnostic: RenderDiagnostic) => void
}) {
  const url = result?.pdf_url
  return <div className="pdf-preview" aria-busy={loading}>
    {error && <div className="pdf-error" role="alert"><strong>PDF 生成失败</strong><p>{error}</p></div>}
    {!loading && result && <>
      <div className={`pdf-quality quality-${result.quality_status}`} role="status">
        <strong>排版质量：{QUALITY_LABEL[result.quality_status]}</strong>
        <span>{result.quality_status === 'passed' ? '通过范围以诊断覆盖为准。' : url ? 'PDF 可以查看和下载，质量结论仍需按问题检查。' : '本次未生成可用 PDF，请按诊断检查后重新预览。'}</span>
        <small>生成器 {result.generator_version ?? '未知'} · 诊断 {result.diagnostics_version ?? '未知'}</small>
      </div>
      {result.diagnostics.length > 0 && <div className="pdf-diagnostics"><strong>诊断 · 点击定位原图</strong><ul>
        {result.diagnostics.map((diagnostic, index) => <li key={`${diagnostic.code}-${diagnostic.page_number}-${index}`}>
          <button className={`diagnostic diagnostic-${diagnostic.severity}`} onClick={() => onDiagnostic(diagnostic)}>
            <span>{diagnostic.severity === 'error' ? '错误' : diagnostic.severity === 'warning' ? '提醒' : '信息'} · 源文件第 {diagnostic.source_page} 页 · 编排第 {diagnostic.arrangement_position} 页{diagnostic.output_page_start != null && ` · 输出 ${diagnostic.output_page_start}${diagnostic.output_page_end !== diagnostic.output_page_start ? `—${diagnostic.output_page_end}` : ''}`}</span>
            <strong>{diagnostic.message}</strong><span>{diagnostic.suggestion}</span>
            <small>{diagnostic.code} · 内容 r{diagnostic.content_revision} / 布局 r{diagnostic.layout_revision} · 检测覆盖 {diagnostic.coverage} · {diagnostic.basis}</small>
          </button>
        </li>)}
      </ul></div>}
      {result.warnings.length > 0 && <details className="pdf-warnings"><summary>编译提醒 · {result.warnings.length} 项</summary><ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
      {result.page_map.length > 0 && <details className="pdf-page-map"><summary>实际源页与输出范围 · {result.page_map.length} 个源页</summary><div className="page-map-table"><table><thead><tr><th>源页 / 编排</th><th>输出范围</th><th>策略 / 修订</th><th>输出纸张 / 比例</th></tr></thead><tbody>
        {result.page_map.map((entry) => <tr key={`${entry.page_number}-${entry.arrangement_position}`}><td>源 {entry.source_page} / 编排 {entry.arrangement_position}</td><td>{entry.output_page_start}—{entry.output_page_end}</td><td>{entry.render_strategy}<br />内容 r{entry.content_revision} / 布局 r{entry.layout_revision}</td><td>{entry.output_width_bp == null || entry.output_height_bp == null ? '尺寸未知' : `${entry.output_width_bp.toFixed(2)} × ${entry.output_height_bp.toFixed(2)} bp`}<br />{entry.canvas_scale == null ? '比例未知 / 源码控制' : `原页 ${Math.round(entry.canvas_scale * 10000) / 100}%`}</td></tr>)}
      </tbody></table></div></details>}
    </>}
    {loading ? <p className="pdf-placeholder" role="status">正在编译 PDF…</p>
      : url ? <details className="full-pdf-view"><summary>查看完整 PDF</summary><iframe src={url} title={title} /><p className="pdf-viewer-help"><a href={url} target="_blank" rel="noreferrer">打开完整 PDF</a> · <a href={url} download>下载 PDF</a></p></details>
        : !error && !result && <p className="pdf-placeholder">{emptyMessage}</p>}
  </div>
}
