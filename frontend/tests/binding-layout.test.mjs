import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { isLatexDocument } from '../src/latexDocument.ts'

const paperSource = readFileSync(new URL('../src/paper.ts', import.meta.url), 'utf8').replaceAll('\r\n', '\n')
const paperContext = vm.createContext({ isLatexDocument })
const bindingStart = paperSource.indexOf('export function bindingPageSide(')
const bindingEnd = paperSource.indexOf('\n}\n', bindingStart) + 2
vm.runInContext(stripTypeScriptTypes(`${paperSource.slice(bindingStart, bindingEnd).replace('export ', '')}\nglobalThis.bindingPageSide = bindingPageSide`), paperContext)
const { bindingPageSide } = paperContext

const page = (pageSide, footerText = '12', pageKind = 'content') => ({
  page_kind: pageKind,
  page_side: pageSide,
  footer_segments: footerText === null ? [] : [{ text: footerText }],
  render_strategy: 'legacy_template', text: '正文片段',
})

test('only content pages with known sides and substantive footers support binding', () => {
  const samples = [
    [page('left'), 'left'],
    [page('right', '  13  '), 'right'],
    [page('left', '\t\n　'), 'unknown'],
    [page('right', null), 'unknown'],
    [page('unknown'), 'unknown'],
    [page(undefined), 'unknown'],
    [page('left', '12', 'front_cover'), 'unknown'],
    [page('right', '13', 'back_cover'), 'unknown'],
  ]
  const before = structuredClone(samples)
  for (const [sample, expected] of samples) assert.equal(bindingPageSide(sample), expected)
  assert.deepEqual(samples, before, 'The display rule must not change recognition data')
})

test('fidelity binding uses known page sides in project paper mode even without legacy footers', () => {
  const fidelity = { ...page('left', null), render_strategy: 'source_fidelity', text: '\\documentclass{article}\n\\begin{document}生成源码\\end{document}' }
  assert.equal(bindingPageSide(fidelity, 'project'), 'left')
  assert.equal(bindingPageSide(fidelity, 'source'), 'unknown')
  assert.equal(bindingPageSide({ ...fidelity, page_side: 'unknown' }, 'project'), 'unknown')
  assert.equal(bindingPageSide({ ...fidelity, render_strategy: 'custom_latex' }, 'project'), 'unknown')
})

test('complete custom documents with preclass commands control their own printing', () => {
  const full = '\\RequirePackage{fix-cm}\n\\PassOptionsToClass{a5paper}{article}\n\\documentclass{article}\n\\begin{document}X\\end{document}'
  assert.equal(bindingPageSide({ ...page('right'), render_strategy: 'custom_latex', text: full }), 'unknown')
  assert.equal(bindingPageSide({ ...page('right'), render_strategy: 'custom_latex', text: '正文片段' }), 'right')
})

// Exercise current PDF/LaTeX state and export using a memory API. Browser
// coverage is separate; no real project, compiler or model is contacted here.
const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8').replaceAll('\r\n', '\n')

function previewState(pages, requested = true, fidelityPaper = 'project') {
  const state = {
    detail: { book: { id: 'test', title: 'Test', layout: { source_fidelity_paper: fidelityPaper } }, pages },
    printVersion: requested, bindingPageSide, exportedVersions: [],
    setActionBusy() {}, setNotice() {}, safeFilename: (value) => value,
    errorText: (error) => error.message, downloads: [],
  }
  state.api = { exportLatex: async (_, enabled) => { state.exportedVersions.push(enabled); return { blob: 'latex source', extension: '.tex' } } }
  state.downloadBlob = (name, blob) => state.downloads.push({ name, blob })
  const context = vm.createContext(state)
  state.derivePreview = () => {
    for (const name of ['bindingPageCount', 'effectivePrintVersion']) {
      const declaration = source.match(new RegExp(`^  const ${name} = (.+)$`, 'm'))
      assert.ok(declaration, `${name} must be derived from the current project`)
      vm.runInContext(stripTypeScriptTypes(`globalThis.${name} = ${declaration[1]}`), context)
    }
  }
  state.derivePreview()
  const prefix = '  const exportLatex = '
  const start = source.indexOf(prefix)
  const end = source.indexOf('\n\n  ', start)
  assert.ok(start >= 0 && end > start, 'The export callback must exist')
  vm.runInContext(stripTypeScriptTypes(`globalThis.exportLatex = ${source.slice(start + prefix.length, end)}`), context)
  return state
}

test('a previously enabled option becomes effectively off with zero usable pages', async () => {
  const state = previewState([page('unknown'), page('left', '   '), page('right', null)])
  assert.equal(state.printVersion, true)
  assert.equal(state.bindingPageCount, 0)
  assert.equal(state.effectivePrintVersion, false)
  await state.exportLatex()
  assert.deepEqual(state.exportedVersions, [false])
  assert.deepEqual(state.downloads, [{ name: 'Test.tex', blob: 'latex source' }])
})

test('availability counts only usable selected pages and respects the user switch', async () => {
  const pages = [page('left'), page('right'), page('unknown'), page('left', '', 'front_cover')]
  for (const requested of [false, true]) {
    const state = previewState(pages, requested)
    assert.equal(state.bindingPageCount, 2)
    assert.equal(state.effectivePrintVersion, requested)
    await state.exportLatex()
    assert.deepEqual(state.exportedVersions, [requested])
  }
})

test('LaTeX export follows refreshed saved pages when usable sides disappear', async () => {
  const state = previewState([page('left')])
  assert.equal(state.effectivePrintVersion, true)
  state.detail = { ...state.detail, pages: [page('unknown')] }
  state.derivePreview()
  await state.exportLatex()
  assert.deepEqual(state.exportedVersions, [false])
})

test('source-sized fidelity output disables the effective printing switch', async () => {
  const state = previewState([{ ...page('right', null), render_strategy: 'source_fidelity' }], true, 'source')
  assert.equal(state.bindingPageCount, 0)
  assert.equal(state.effectivePrintVersion, false)
  await state.exportLatex()
  assert.deepEqual(state.exportedVersions, [false])
})
