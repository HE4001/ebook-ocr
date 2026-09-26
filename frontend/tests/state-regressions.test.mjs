import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { fileSubtree, findFilePageAnchor, insertPageBlock, moveFileTree } from '../src/organizerOrder.ts'

// Run the real component callbacks with in-memory state and API responses. This
// needs Node 22.18+ and avoids adding a DOM test dependency or calling the server.
const readSource = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8').replaceAll('\r\n', '\n')
const appSource = readSource('App.tsx')
const organizerSource = readSource('ProjectOrganizer.tsx')

function installCallback(context, source, name, declaration = 'const') {
  const prefix = `  ${declaration} ${name}${declaration === 'const' ? ' = ' : '('}`
  const start = source.indexOf(prefix)
  assert.notEqual(start, -1, `Callback ${name} must exist`)
  const end = source.indexOf('\n\n  ', start)
  assert.notEqual(end, -1, `Callback ${name} must have a declaration boundary`)
  const callback = source.slice(start, end)
  const runnable = declaration === 'const'
    ? `globalThis.${name} = ${callback.slice(prefix.length)}`
    : `${callback}\nglobalThis.${name} = ${name}`
  vm.runInContext(stripTypeScriptTypes(runnable), context)
}

function organizerState({ files = ['A', 'B', 'C'], parents = { A: null, B: null, C: null }, layout = [1, 3, 5, 2, 4], selected = [1, 3, 5] } = {}) {
  const pages = [
    { number: 1, source_id: 'A' }, { number: 2, source_id: 'A' },
    { number: 3, source_id: 'B' }, { number: 4, source_id: 'B' },
    { number: 5, source_id: 'C' },
  ]
  const state = {
    fileOrder: files, parents, layout, order: selected, included: new Set(selected),
    pageById: new Map(pages.map((page) => [page.number, page])),
    fileById: new Map(files.map((id) => [id, { filename: id }])),
    fileSubtree, findFilePageAnchor, insertPageBlock, moveFileTree,
    announce() {}, chooseSource() {}, CHILD_FILE_HELP: 'Child files cannot contain children',
  }
  for (const [setter, key] of [['setFileOrder', 'fileOrder'], ['setParents', 'parents'], ['setLayout', 'layout'], ['setIncluded', 'included']]) {
    state[setter] = (value) => { state[key] = value }
  }
  const context = vm.createContext(state)
  for (const name of ['pagesForFile', 'moveFile', 'commitFileInsert']) installCallback(context, organizerSource, name, 'function')
  return state
}

const finalOrder = (state) => Array.from(state.layout).filter((id) => state.included.has(id))

test('moving a partially selected file down uses selected target pages', () => {
  const state = organizerState()
  state.moveFile('A', null, 'B', 'after')
  assert.deepEqual(Array.from(state.fileOrder), ['B', 'A', 'C'])
  assert.deepEqual(finalOrder(state), [3, 1, 5])
  assert.deepEqual([...state.included], [1, 3, 5])
  assert.deepEqual([...state.layout].sort(), [1, 2, 3, 4, 5])
})

test('moving past a file with no selected pages stays before the following file', () => {
  const state = organizerState({ layout: [1, 5, 2, 3, 4], selected: [1, 5] })
  state.moveFile('A', null, 'B', 'after')
  assert.deepEqual(Array.from(state.fileOrder), ['B', 'A', 'C'])
  assert.deepEqual(finalOrder(state), [1, 5])
})

test('embedding at file end ignores excluded parent pages', () => {
  const state = organizerState()
  state.commitFileInsert('A', 'B', null, 'after', false)
  assert.equal(state.parents.A, 'B')
  assert.deepEqual(finalOrder(state), [3, 1, 5])
  assert.deepEqual([...state.included], [1, 3, 5])
})

test('moving a parent carries its selected child pages without restoring excluded pages', () => {
  const state = organizerState({ parents: { A: null, B: 'A', C: null }, layout: [1, 3, 5, 2, 4] })
  state.moveFile('A', null, 'C', 'after')
  assert.deepEqual(Array.from(state.fileOrder), ['C', 'A', 'B'])
  assert.deepEqual(finalOrder(state), [5, 1, 3])
  assert.deepEqual([...state.included], [1, 3, 5])
})

test('detaching a selected child still moves its pages to the book end', () => {
  const state = organizerState({ parents: { A: null, B: 'A', C: null } })
  state.moveFile('B', null)
  assert.deepEqual(Array.from(state.fileOrder), ['A', 'C', 'B'])
  assert.equal(state.parents.B, null)
  assert.deepEqual(finalOrder(state), [1, 5, 3])
})

function appState(status = 'processing') {
  const page = {
    number: 1, source_filename: 'test.pdf', source_page: 1, status,
    text: '', page_kind: 'content', cover_fields: [], header_segments: [], footer_segments: [],
    error: status === 'failed' ? 'OCR failed' : null,
  }
  const book = { id: 'book', status, completed_pages: 0, paper_size: 'a4', error: status === 'failed' ? '1 页处理失败' : null }
  const state = {
    detail: { book, files: [], pages: [page] }, books: [book], sourcePage: page,
    selectedBookIdRef: { current: 'book' }, detailRequestRef: { current: 0 }, selectedPageNumber: 1,
    layoutSaving: false, editLocked: status === 'processing', dirty: true, pageDraft: { text: '未保存草稿', page_kind: 'content', cover_fields: [] },
    api: {}, PAPER_SIZES: { a5: { label: 'A5' } }, useCallback: (callback) => callback,
    errorText: (error) => error.message, sourceLabel: (saved) => saved.source_filename,
    draftFromPage: (saved) => ({ text: saved.text, page_kind: saved.page_kind, cover_fields: saved.cover_fields }),
  }
  for (const [setter, key] of [
    ['setDetail', 'detail'], ['setBooks', 'books'], ['setSelectedPageNumber', 'selectedPageNumber'],
    ['setLayoutSaving', 'layoutSaving'], ['setDetailLoading', 'detailLoading'], ['setNotice', 'notice'],
    ['setSaving', 'saving'], ['setDirty', 'dirty'], ['setPageDraft', 'pageDraft'],
  ]) state[setter] = (value) => { state[key] = typeof value === 'function' ? value(state[key]) : value }
  const context = vm.createContext(state)
  for (const name of ['applyDetail', 'loadDetail', 'savePaperSize', 'savePage']) installCallback(context, appSource, name)
  return state
}

const completedDetail = (state, paperSize = 'a5') => ({
  ...state.detail,
  book: { ...state.detail.book, status: 'ready', completed_pages: 1, paper_size: paperSize, error: null },
  pages: [{ ...state.sourcePage, status: 'ready', text: '已识别正文', error: null }],
})

test('paper save at processing completion refreshes pages and preserves an unsaved draft', async () => {
  const state = appState()
  const fresh = completedDetail(state)
  const draft = state.pageDraft
  let resolveOldPoll
  const oldPollResponse = new Promise((resolve) => { resolveOldPoll = resolve })
  let reads = 0
  state.api.getBook = async () => ++reads === 1 ? oldPollResponse : fresh
  state.api.saveBookLayout = async () => fresh.book
  const oldDetail = structuredClone(state.detail)
  const oldPoll = state.loadDetail('book', true)
  await state.savePaperSize('a5')
  resolveOldPoll(oldDetail)
  await oldPoll
  assert.equal(reads, 2)
  assert.equal(state.detail.book.status, 'ready')
  assert.equal(state.detail.pages[0].status, 'ready')
  assert.equal(state.detail.pages[0].text, '已识别正文')
  assert.equal(state.books[0].completed_pages, 1)
  assert.equal(state.pageDraft, draft)
  assert.equal(state.dirty, true)
})

test('a failed detail refresh after saving paper keeps processing eligible for polling', async () => {
  const state = appState()
  const fresh = completedDetail(state)
  state.api.saveBookLayout = async () => fresh.book
  state.api.getBook = async () => { throw new Error('temporary read failure') }
  await state.savePaperSize('a5')
  assert.equal(state.detail.book.paper_size, 'a5')
  assert.equal(state.detail.book.status, 'processing')
  assert.equal(state.layoutSaving, false)
  assert.equal(state.detail.pages[0].status, 'processing')
  assert.equal(state.dirty, true)
})

test('manual correction refreshes project completion, errors and sidebar summary', async () => {
  const state = appState('failed')
  const fresh = completedDetail(state, 'a4')
  fresh.pages[0].text = state.pageDraft.text
  let reads = 0
  state.api.savePage = async () => fresh.pages[0]
  state.api.getBook = async () => { reads += 1; return fresh }
  await state.savePage()
  assert.equal(reads, 1)
  assert.equal(state.detail.pages[0].status, 'ready')
  assert.equal(state.detail.book.status, 'ready')
  assert.equal(state.detail.book.completed_pages, 1)
  assert.equal(state.detail.book.error, null)
  assert.equal(state.books[0].status, 'ready')
  assert.equal(state.books[0].completed_pages, 1)
  assert.equal(state.books[0].error, null)
  assert.equal(state.dirty, false)
})
