import assert from 'node:assert/strict'
import test from 'node:test'
import { calibrationFromPage, removeLayoutLine, splitLayoutLine, startCalibration } from '../src/layoutDraft.ts'
import { isLatexDocument } from '../src/latexDocument.ts'
import { outputBoxToNormalized, outputPointToSource, sourceBoxToOutput, sourcePointToOutput } from '../src/proofingGeometry.ts'

const observation = () => ({
  schema_version: 1, body_frame: null,
  regions: [{ region_id: 'body', kind: 'body', order: 0, bbox: null, parent_id: null, basis: null }],
  lines: [
    { line_id: 'a', block_id: 'body', order: 3, kind: 'equation', latex: 'a=b+c', bbox: [0.1, 0.2, 0.8, 0.3], baseline: 0.28,
      style: { font_family: null, font_size_bp: null, font_size_ratio: null, bold: null, italic: null, basis: null }, basis: 'model_estimate' },
    { line_id: 'b', block_id: 'body', order: 9, kind: 'text', latex: '下一行全部字符', bbox: null, baseline: null,
      style: { font_family: 'songti', font_size_bp: 10, font_size_ratio: null, bold: false, italic: null, basis: 'manual' }, basis: null },
  ],
  equation_groups: [{ group_id: 'group', line_ids: ['a'], bbox: null, align_x: 0.4, number: { latex: '(1)', line_id: 'a', bbox: null, anchor_x: 0.9 }, basis: null }],
  review_reasons: ['字体待人工校准'],
})

test('initial calibration preserves observation and omits manual canvas/font overrides', () => {
  const original = observation()
  const page = { content_revision: 12, layout_revision: 8, layout_source: { ...original,
    canvas_width_bp: 392, canvas_height_bp: 576, canvas_basis: 'project', body_font_size_bp: 10, body_font_family: 'kaiti', body_font_basis: 'model_estimate' } }
  const draft = calibrationFromPage(page)
  assert.deepEqual(draft.observation, original)
  assert.equal(draft.expected_content_revision, 12)
  assert.equal(draft.expected_layout_revision, 8)
  for (const key of ['canvas_width_bp', 'canvas_height_bp', 'body_font_size_bp', 'body_font_family']) assert.equal(Object.hasOwn(draft, key), false)
})

test('starting calibration preserves the entire existing fragment and invents no geometry', () => {
  const page = { text: '原文第一行\n\\[a=b\\]\n尾行', content_revision: 3, layout_revision: 0 }
  const draft = startCalibration(page, 'original')
  assert.equal(draft.observation.lines.length, 1)
  assert.equal(draft.observation.lines[0].latex, page.text)
  assert.equal(draft.observation.lines[0].bbox, null)
  assert.equal(draft.observation.lines[0].baseline, null)
  assert.equal(draft.observation.body_frame, null)
  assert.equal(draft.observation.regions[0].bbox, null)
  assert.ok(draft.observation.review_reasons.length)
})

test('splitting one line retains every character, style and formula relationship', () => {
  const original = observation()
  const before = structuredClone(original)
  const split = splitLayoutLine(original, 'a', 2, 'new')
  assert.deepEqual(original, before)
  assert.equal(split.lines.map((line) => line.latex).join(''), original.lines.map((line) => line.latex).join(''))
  assert.deepEqual(split.lines.map((line) => line.line_id), ['a', 'new', 'b'])
  assert.equal(split.lines[0].bbox, null)
  assert.equal(split.lines[1].baseline, null)
  assert.deepEqual(split.lines[1].style, original.lines[0].style)
  assert.deepEqual(split.equation_groups[0].line_ids, ['a', 'new'])
  assert.deepEqual(split.equation_groups[0].number, original.equation_groups[0].number)
  assert.deepEqual(split.regions, original.regions)
  assert.deepEqual(split.review_reasons, original.review_reasons)
})

test('explicit deletion removes only that line and now-empty formula references', () => {
  const original = observation()
  const updated = removeLayoutLine(original, 'a')
  assert.deepEqual(updated.lines, [original.lines[1]])
  assert.deepEqual(updated.equation_groups, [])
  assert.deepEqual(updated.regions, original.regions)
  assert.equal(original.lines.length, 2)
})

test('document recognition accepts supported preclass commands and ignores printed snippets', () => {
  for (const source of [
    '\\documentclass{article}',
    '% 注释\r\n\\RequirePackage[2020]{fix-cm}[2021]\n\\documentclass[a5paper]{article}',
    '\\PassOptionsToPackage{unicode}{hyperref}\n\\PassOptionsToClass{a4paper}{article}\n\\documentclass{article}',
  ]) assert.equal(isLatexDocument(source), true)
  assert.equal(isLatexDocument('这里打印 \\documentclass{article}'), false)
  assert.equal(isLatexDocument('\\[a=b\\]'), false)
})

const mapping = { source_to_output_affine: [200, 0, 0, 300, 20, 30], output_width_bp: 240, output_height_bp: 360 }

test('position synchronization uses the affine page coordinate transform including margins', () => {
  const source = { x: 0.2, y: 0.7 }
  const output = sourcePointToOutput(source, mapping)
  assert.equal(output.x, 0.25)
  assert.equal(output.y, 2 / 3)
  const roundtrip = outputPointToSource(output, mapping)
  assert.ok(Math.abs(roundtrip.x - source.x) < 1e-10)
  assert.ok(Math.abs(roundtrip.y - source.y) < 1e-10)
})

test('diagnostic highlights map all four corners under a rotated affine', () => {
  const rotated = { source_to_output_affine: [0, 200, -300, 0, 300, 10], output_width_bp: 320, output_height_bp: 240 }
  const sourceBox = [0.1, 0.2, 0.4, 0.5]
  const box = sourceBoxToOutput(sourceBox, rotated)
  assert.deepEqual(box, [150 / 320, 30 / 240, 240 / 320, 90 / 240])
  assert.deepEqual(outputBoxToNormalized([150, 30, 240, 90], rotated), box)
})
