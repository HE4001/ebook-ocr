import { useEffect, useRef, useState } from 'react'
import { removeLayoutLine, splitLayoutLine, startCalibration, uncalibratedLine } from './layoutDraft'
import { isLatexDocument } from './latexDocument'
import type { BBox, EquationGroup, FontFamily, LayoutCalibrationUpdate, LayoutLine, Page } from './types'

function OptionalNumber({ label, value, onChange, min = 0, max, required = false }: {
  label: string; value: number | null | undefined; onChange: (value: number | null) => void
  min?: number; max?: number; required?: boolean
}) {
  return <label><span>{label}</span><input type="number" min={min} max={max} step="any" value={value ?? ''}
    placeholder="未知" required={required} onChange={(event) => onChange(event.target.value === '' ? null : Number(event.target.value))} /></label>
}

function BoxInput({ label, value, onChange, onEditing }: {
  label: string; value: BBox | null; onChange: (value: BBox | null) => void; onEditing: () => void
}) {
  const [coordinates, setCoordinates] = useState(() => value?.map(String) ?? ['', '', '', ''])
  useEffect(() => { setCoordinates(value?.map(String) ?? ['', '', '', '']) }, [value])
  const partial = coordinates.some(Boolean)
  return <div className="calibration-box"><span>{label} · 归一坐标 0—1</span><div>
    {['左 x₀', '上 y₀', '右 x₁', '下 y₁'].map((axis, index) => <label key={axis}><span>{axis}</span>
      <input aria-label={`${label} ${axis}`} type="number" min={0} max={1} step="any" required={partial}
        value={coordinates[index]} placeholder="未知" onChange={(event) => {
          const next = coordinates.map((coordinate, position) => position === index ? event.target.value : coordinate)
          setCoordinates(next)
          onEditing()
          if (next.every((coordinate) => coordinate === '')) onChange(null)
          else if (next.every((coordinate) => coordinate !== '')) onChange(next.map(Number) as BBox)
        }} /></label>)}
  </div><small>四项均留空保留未知；局部填写后请补齐四边。原点在左上角。</small></div>
}

export function LayoutCalibration({ page, draft, dirty, disabled, saving, onChange, onEditing, onSave, onPreview, onRestore, onLocate }: {
  page: Page; draft: LayoutCalibrationUpdate | null; dirty: boolean; disabled: boolean; saving: boolean
  onChange: (draft: LayoutCalibrationUpdate) => void; onEditing: () => void
  onSave: () => void; onPreview: () => void; onRestore: () => void
  onLocate: (bbox: BBox | null, lineId?: string) => void
}) {
  const form = useRef<HTMLFormElement>(null)
  const lineInput = useRef<HTMLTextAreaElement>(null)
  const [lineId, setLineId] = useState(draft?.observation.lines[0]?.line_id ?? '')
  const [groupId, setGroupId] = useState(draft?.observation.equation_groups[0]?.group_id ?? '')
  const fullDocument = isLatexDocument(page.text)

  if (page.page_kind !== 'content') return <p className="calibration-note">封面和封底请使用书目信息与源码编辑；正文原行校准只适用于正文页。</p>
  if (!draft) return <div className="calibration-empty">
    <strong>本页缺少原书布局，尚未还原</strong>
    <p>可沿用现有正文开始校准，再逐行拆分和补充几何信息，无需重新录入整页。只提供了源码时，程序无法确定原图中的断行和公式关系。</p>
    {fullDocument ? <p>本页是完整 LaTeX 文档，请继续使用自由源码编辑。若已有原书布局，可先恢复该布局后校准。</p>
      : <button disabled={disabled} onClick={() => {
        const id = `line-${crypto.randomUUID()}`
        onChange(startCalibration(page, id)); setLineId(id)
      }}>沿用现有正文开始校准</button>}
  </div>

  const observation = draft.observation
  const orderedLines = [...observation.lines].sort((left, right) => left.order - right.order)
  const line = observation.lines.find((item) => item.line_id === lineId) ?? observation.lines[0]
  const group = observation.equation_groups.find((item) => item.group_id === groupId) ?? observation.equation_groups[0]
  const changeObservation = (changes: Partial<typeof observation>) => onChange({ ...draft, observation: { ...observation, ...changes } })
  const changeLine = (changes: Partial<LayoutLine>) => changeObservation({
    lines: observation.lines.map((item) => item.line_id === line.line_id ? { ...item, ...changes } : item),
  })
  const changeGroup = (changes: Partial<EquationGroup>) => changeObservation({
    equation_groups: observation.equation_groups.map((item) => item.group_id === group.group_id ? { ...item, ...changes } : item),
  })
  const addLine = () => {
    const id = `line-${crypto.randomUUID()}`
    const blockId = line?.block_id ?? observation.regions[0]?.region_id ?? id
    changeObservation({
      regions: observation.regions.length ? observation.regions : [{ region_id: blockId, kind: 'body', order: 0, bbox: null, parent_id: null, basis: null }],
      lines: [...observation.lines, uncalibratedLine('', id, Math.max(-1, ...observation.lines.map((item) => item.order)) + 1, blockId)],
    })
    setLineId(id)
  }
  const numberedGroup = group?.number
  const region = line && observation.regions.find((item) => item.region_id === line.block_id)
  const canvasWidth = draft.canvas_width_bp !== undefined ? draft.canvas_width_bp : page.layout_source?.canvas_width_bp ?? null
  const canvasHeight = draft.canvas_height_bp !== undefined ? draft.canvas_height_bp : page.layout_source?.canvas_height_bp ?? null
  const fontSize = draft.body_font_size_bp !== undefined ? draft.body_font_size_bp : page.layout_source?.body_font_size_bp ?? null
  const fontFamily = draft.body_font_family !== undefined ? draft.body_font_family : page.layout_source?.body_font_family ?? null
  const confirmCanvas = (width: number | null, height: number | null) => onChange({ ...draft, canvas_width_bp: width, canvas_height_bp: height })
  const confirmFont = (size: number | null, family: FontFamily | null) => onChange({ ...draft, body_font_size_bp: size, body_font_family: family })

  return <form ref={form} className="layout-calibration" onSubmit={(event) => { event.preventDefault(); onSave() }}>
    <fieldset disabled={disabled}>
      <legend>原书布局校准</legend>
      <p className="calibration-note">保存校准后自动生成 LaTeX 源码。预览使用当前校准草稿，不保存修改。留空表示未知；全局模板行距不会替换原页基线。</p>
      {observation.review_reasons.length > 0 && <ul className="calibration-review">{observation.review_reasons.map((reason, index) => <li key={index}><span>{reason}</span><button type="button" onClick={() => changeObservation({ review_reasons: observation.review_reasons.filter((_, position) => position !== index) })}>此项已人工核对</button></li>)}</ul>}
      <BoxInput label="版心" value={observation.body_frame} onChange={(body_frame) => changeObservation({ body_frame })} onEditing={onEditing} />
      <div className="calibration-fields">
        <OptionalNumber label="原页画布宽（bp）" value={canvasWidth} min={0.01} required={canvasHeight != null} onChange={(width) => confirmCanvas(width, canvasHeight)} />
        <OptionalNumber label="原页画布高（bp）" value={canvasHeight} min={0.01} required={canvasWidth != null} onChange={(height) => confirmCanvas(canvasWidth, height)} />
        <OptionalNumber label="正文字号（bp）" value={fontSize} min={0.01} max={200} onChange={(size) => confirmFont(size, fontFamily)} />
        <label><span>正文原字族</span><select value={fontFamily ?? ''} onChange={(event) => confirmFont(fontSize, (event.target.value || null) as FontFamily | null)}><option value="">未知</option><option value="songti">宋体</option><option value="heiti">黑体</option><option value="kaiti">楷体</option></select></label>
      </div>
      <div className="calibration-line-tools"><button type="button" disabled={canvasWidth == null || canvasHeight == null} onClick={() => confirmCanvas(canvasWidth, canvasHeight)}>已核对当前画布尺寸</button><span>原依据：{page.layout_source?.canvas_basis ?? '未知'}</span><button type="button" disabled={fontSize == null && fontFamily == null} onClick={() => confirmFont(fontSize, fontFamily)}>已核对当前正文字体</button><span>原依据：{page.layout_source?.body_font_basis ?? '未知'}</span></div>
      <p className="calibration-note">画布与正文字体仅在实际编辑或点击确认后作为人工校准提交；直接预览或保存其他字段保留原依据。</p>
      <section className="calibration-section">
        <div className="calibration-section-title"><h3>原行 · {observation.lines.length} 行</h3><button type="button" onClick={addLine}>新增行</button></div>
        {line && <>
          <label><span>选择要校准的原行</span><select value={line.line_id} onChange={(event) => setLineId(event.target.value)}>{orderedLines.map((item) => <option key={item.line_id} value={item.line_id}>{item.order + 1} · {item.kind} · {item.latex.slice(0, 60) || '空行'}</option>)}</select></label>
          <div className="calibration-line-tools"><button type="button" onClick={() => onLocate(line.bbox, line.line_id)}>定位原图区域</button><button type="button" onClick={() => {
            onChange({ ...draft, observation: splitLayoutLine(observation, line.line_id, lineInput.current!.selectionStart, `line-${crypto.randomUUID()}`) })
          }}>在光标处拆为两行</button><button type="button" onClick={() => {
            if (window.confirm('删除此原行及其内容？相关公式组引用将同步移除。')) onChange({ ...draft, observation: removeLayoutLine(observation, line.line_id) })
          }}>删除选中行</button></div>
          <label><span>本行 LaTeX 内容</span><textarea ref={lineInput} rows={3} value={line.latex} spellCheck={false} onChange={(event) => changeLine({ latex: event.target.value })} /></label>
          <p className="calibration-note">拆行保留全部字符，并将新行几何恢复为未知；请按原图核对公式环境和断行。公式组内每行用数学内容，编号在组中单独填写。</p>
          {region && <><BoxInput key={`region-${region.region_id}`} label={`所属区域（${region.kind}）`} value={region.bbox} onChange={(bbox) => changeObservation({ regions: observation.regions.map((item) => item.region_id === region.region_id ? { ...item, bbox, basis: 'manual' } : item) })} onEditing={onEditing} /><p className="calibration-note">所属区域独立于全页版心；原行先受其所属区域约束。编辑区域只更新本区域边界，保留类别、父级和所有原行。</p></>}
          <BoxInput key={`line-${line.line_id}`} label="本行区域" value={line.bbox} onChange={(bbox) => changeLine({ bbox, basis: 'manual' })} onEditing={onEditing} />
          <div className="calibration-fields">
            <OptionalNumber label="本行基线 y" value={line.baseline} max={1} onChange={(baseline) => changeLine({ baseline, basis: 'manual' })} />
            <label><span>本行类别</span><select value={line.kind} disabled={observation.equation_groups.some((item) => item.line_ids.includes(line.line_id))} onChange={(event) => changeLine({ kind: event.target.value as LayoutLine['kind'] })}>{['text', 'equation', 'header', 'footer', 'page_number', 'table', 'footnote', 'caption'].map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></label>
            <OptionalNumber label="本行字号（bp）" value={line.style.font_size_bp} min={0.01} max={200} onChange={(font_size_bp) => changeLine({ style: { ...line.style, font_size_bp, basis: 'manual' } })} />
            <OptionalNumber label="本行字号相对正文" value={line.style.font_size_ratio} min={0.01} onChange={(font_size_ratio) => changeLine({ style: { ...line.style, font_size_ratio, basis: 'manual' } })} />
            <label><span>本行字族</span><select value={line.style.font_family ?? ''} onChange={(event) => changeLine({ style: { ...line.style, font_family: (event.target.value || null) as FontFamily | null, basis: 'manual' } })}><option value="">未知 / 正文设置</option><option value="songti">宋体</option><option value="heiti">黑体</option><option value="kaiti">楷体</option></select></label>
            {(['bold', 'italic'] as const).map((property) => <label key={property}><span>{property === 'bold' ? '字重' : '斜体'}</span><select value={line.style[property] == null ? '' : String(line.style[property])} onChange={(event) => changeLine({ style: { ...line.style, [property]: event.target.value === '' ? null : event.target.value === 'true', basis: 'manual' } })}><option value="">未知</option><option value="false">否</option><option value="true">是</option></select></label>)}
          </div>
        </>}
      </section>
      <section className="calibration-section">
        <div className="calibration-section-title"><h3>公式组 · {observation.equation_groups.length} 组</h3><button type="button" disabled={!line || observation.equation_groups.some((item) => item.line_ids.includes(line.line_id))} onClick={() => {
          const id = `equation-${crypto.randomUUID()}`
          changeObservation({ lines: observation.lines.map((item) => item.line_id === line.line_id ? { ...item, kind: 'equation' } : item),
            equation_groups: [...observation.equation_groups, { group_id: id, line_ids: [line.line_id], bbox: null, align_x: null, number: null, basis: null }] })
          setGroupId(id)
        }}>选中行建立公式组</button></div>
        {group && <>
          <label><span>选择公式组</span><select value={group.group_id} onChange={(event) => setGroupId(event.target.value)}>{observation.equation_groups.map((item) => <option key={item.group_id} value={item.group_id}>{item.group_id} · {item.line_ids.length} 行</option>)}</select></label>
          <div className="group-line-picker"><span>组内原行（按阅读顺序）</span>{orderedLines.filter((item) => item.kind === 'equation').map((item) => <label className="check-row" key={item.line_id}><input type="checkbox" checked={group.line_ids.includes(item.line_id)} disabled={(group.line_ids.length === 1 && group.line_ids.includes(item.line_id)) || observation.equation_groups.some((other) => other.group_id !== group.group_id && other.line_ids.includes(item.line_id))} onChange={(event) => {
            const ids = event.target.checked ? [...group.line_ids, item.line_id] : group.line_ids.filter((id) => id !== item.line_id)
            changeGroup({ line_ids: orderedLines.filter((candidate) => ids.includes(candidate.line_id)).map((candidate) => candidate.line_id), number: group.number && ids.includes(group.number.line_id) ? group.number : null })
          }} /><span>{item.order + 1} · {item.latex.slice(0, 80)}</span></label>)}</div>
          <BoxInput key={`group-${group.group_id}`} label="公式组区域" value={group.bbox} onChange={(bbox) => changeGroup({ bbox, basis: 'manual' })} onEditing={onEditing} />
          <OptionalNumber label="对齐锚点 x" value={group.align_x} max={1} onChange={(align_x) => changeGroup({ align_x, basis: 'manual' })} />
          <label><span>公式编号（留空表示无编号）</span><input value={numberedGroup?.latex ?? ''} onChange={(event) => changeGroup({ number: event.target.value ? { latex: event.target.value, line_id: numberedGroup?.line_id ?? group.line_ids[0], bbox: numberedGroup?.bbox ?? null, anchor_x: numberedGroup?.anchor_x ?? null } : null })} /></label>
          {numberedGroup && <div className="calibration-fields"><label><span>编号所属行</span><select value={numberedGroup.line_id} onChange={(event) => changeGroup({ number: { ...numberedGroup, line_id: event.target.value } })}>{group.line_ids.map((id) => <option key={id} value={id}>{id}</option>)}</select></label><OptionalNumber label="编号右缘锚点 x" value={numberedGroup.anchor_x} max={1} onChange={(anchor_x) => changeGroup({ number: { ...numberedGroup, anchor_x } })} /></div>}
          {numberedGroup && <BoxInput key={`number-${group.group_id}`} label="编号区域" value={numberedGroup.bbox} onChange={(bbox) => changeGroup({ number: { ...numberedGroup, bbox } })} onEditing={onEditing} />}
          <button type="button" onClick={() => {
            if (!group.number || window.confirm('解除此公式组会移除组锚点和独立编号；所有原行内容保留。确定继续吗？'))
              changeObservation({ equation_groups: observation.equation_groups.filter((item) => item.group_id !== group.group_id) })
          }}>解除选中公式组（原行内容保留）</button>
        </>}
      </section>
      <div className="calibration-actions"><button className="primary" type="submit" disabled={!dirty}>{saving ? '保存中…' : '保存校准并生成源码'}</button><button type="button" onClick={() => { if (form.current!.reportValidity()) onPreview() }}>预览本页校准草稿</button><button type="button" disabled={!dirty} onClick={onRestore}>恢复已保存布局</button></div>
    </fieldset>
  </form>
}
