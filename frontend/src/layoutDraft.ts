import type { LayoutCalibrationUpdate, LayoutLine, LayoutObservation, Page } from './types'

export function calibrationFromPage(page: Page): LayoutCalibrationUpdate | null {
  const layout = page.layout_source
  if (!layout) return null
  return {
    observation: {
      schema_version: layout.schema_version, body_frame: layout.body_frame,
      regions: layout.regions, lines: layout.lines, equation_groups: layout.equation_groups,
      review_reasons: layout.review_reasons,
    },
    expected_content_revision: page.content_revision,
    expected_layout_revision: page.layout_revision,
    render_strategy: 'source_fidelity',
  }
}

export function uncalibratedLine(latex: string, id: string, order: number, blockId = id): LayoutLine {
  return {
    line_id: id, block_id: blockId, order, kind: 'text', latex, bbox: null, baseline: null,
    style: { font_family: null, font_size_bp: null, font_size_ratio: null, bold: null, italic: null, basis: null },
    basis: null,
  }
}

export function startCalibration(page: Page, lineId: string): LayoutCalibrationUpdate {
  return {
    observation: {
      schema_version: 1, body_frame: null,
      regions: page.text ? [{ region_id: lineId, kind: 'body', order: 0, bbox: null, parent_id: null, basis: null }] : [],
      lines: page.text ? [uncalibratedLine(page.text, lineId, 0)] : [], equation_groups: [],
      review_reasons: ['沿用现有正文：尚未核对原行、公式分组及几何信息'],
    },
    expected_content_revision: page.content_revision,
    expected_layout_revision: page.layout_revision,
    render_strategy: 'source_fidelity',
  }
}

export function splitLayoutLine(observation: LayoutObservation, lineId: string, offset: number, nextId: string): LayoutObservation {
  const orderedLines = [...observation.lines].sort((left, right) => left.order - right.order)
  const index = orderedLines.findIndex((line) => line.line_id === lineId)
  const original = orderedLines[index]
  const first = { ...original, latex: original.latex.slice(0, offset), bbox: null, baseline: null, basis: null }
  const second = { ...original, line_id: nextId, latex: original.latex.slice(offset), bbox: null, baseline: null, basis: null }
  const lines = [...orderedLines.slice(0, index), first, second, ...orderedLines.slice(index + 1)]
    .map((line, order) => ({ ...line, order }))
  return {
    ...observation, lines,
    equation_groups: observation.equation_groups.map((group) => group.line_ids.includes(lineId)
      ? { ...group, bbox: null, line_ids: group.line_ids.flatMap((id) => id === lineId ? [id, nextId] : [id]) }
      : group),
  }
}

export function removeLayoutLine(observation: LayoutObservation, lineId: string): LayoutObservation {
  return {
    ...observation, lines: observation.lines.filter((line) => line.line_id !== lineId),
    equation_groups: observation.equation_groups
      .map((group) => ({ ...group, line_ids: group.line_ids.filter((id) => id !== lineId),
        number: group.number?.line_id === lineId ? null : group.number }))
      .filter((group) => group.line_ids.length > 0),
  }
}
