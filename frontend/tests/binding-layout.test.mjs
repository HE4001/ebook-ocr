import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { bindingPageSide } from '../src/paper.ts'

const page = (pageSide, footerText = '12', pageKind = 'content') => ({
  page_kind: pageKind,
  page_side: pageSide,
  footer_segments: footerText === null ? [] : [{ text: footerText }],
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

// Exercise the current App state derivation and export callback with a memory
// API. Browser coverage is separate; no real project or model is contacted here.
const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8').replaceAll('\r\n', '\n')

function previewState(pages, requested = true) {
  const state = {
    detail: { book: { id: 'test', title: 'Test' }, pages },
    printVersion: requested, bindingPageSide, exportedVersions: [],
    setActionBusy() {}, setNotice() {}, safeFilename: (value) => value,
    errorText: (error) => error.message, downloadText() {},
  }
  state.api = { exportBook: async () => state.detail }
  state.buildStandaloneHtml = (_, enabled) => { state.exportedVersions.push(enabled); return 'test html' }
  const context = vm.createContext(state)
  for (const name of ['bindingPageCount', 'effectivePrintVersion']) {
    const declaration = source.match(new RegExp(`^  const ${name} = (.+)$`, 'm'))
    assert.ok(declaration, `${name} must be derived from the current project`)
    vm.runInContext(stripTypeScriptTypes(`globalThis.${name} = ${declaration[1]}`), context)
  }
  const prefix = '  const exportData = '
  const start = source.indexOf(prefix)
  const end = source.indexOf('\n\n  ', start)
  assert.ok(start >= 0 && end > start, 'The export callback must exist')
  vm.runInContext(stripTypeScriptTypes(`globalThis.exportData = ${source.slice(start + prefix.length, end)}`), context)
  return state
}

test('a previously enabled option becomes effectively off with zero usable pages', async () => {
  const state = previewState([page('unknown'), page('left', '   '), page('right', null)])
  assert.equal(state.printVersion, true)
  assert.equal(state.bindingPageCount, 0)
  assert.equal(state.effectivePrintVersion, false)
  await state.exportData('html')
  assert.deepEqual(state.exportedVersions, [false])
})

test('availability counts only usable selected pages and respects the user switch', async () => {
  const pages = [page('left'), page('right'), page('unknown'), page('left', '', 'front_cover')]
  for (const requested of [false, true]) {
    const state = previewState(pages, requested)
    assert.equal(state.bindingPageCount, 2)
    assert.equal(state.effectivePrintVersion, requested)
    await state.exportData('html')
    assert.deepEqual(state.exportedVersions, [requested])
  }
})

test('HTML export rechecks the latest pages when usable sides disappear', async () => {
  const state = previewState([page('left')])
  assert.equal(state.effectivePrintVersion, true)
  state.api.exportBook = async () => ({ ...state.detail, pages: [page('unknown')] })
  await state.exportData('html')
  assert.deepEqual(state.exportedVersions, [false])
})
