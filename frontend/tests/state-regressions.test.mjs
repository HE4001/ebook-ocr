import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'
import { fileSubtree, findFilePageAnchor, insertPageBlock, moveFileTree } from '../src/organizerOrder.ts'
import { calibrationFromPage } from '../src/layoutDraft.ts'

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
    number: 1, source_id: 'source', source_filename: 'test.pdf', source_page: 1, status,
    text: '', page_kind: 'content', cover_fields: [], header_segments: [], footer_segments: [],
    render_strategy: 'legacy_template', content_revision: 2, layout_revision: 1, layout_source: null,
    error: status === 'failed' ? 'OCR failed' : null,
  }
  const book = { id: 'book', status, completed_pages: 0, paper_size: 'a4', layout: { source_fidelity_paper: 'project' }, error: status === 'failed' ? '1 页处理失败' : null }
  const state = {
    detail: { book, files: [], pages: [page] }, books: [book], sourcePage: page,
    selectedBookIdRef: { current: 'book' }, detailRequestRef: { current: 0 }, selectedPageNumber: 1,
    selectedPageRef: { current: 1 }, previewRequestRef: { current: 0 }, previewKeysRef: { current: { book: 'book-key', page: 'page-key' } },
    bookPdfKey: 'book-key', pagePdfKey: 'page-key', effectivePrintVersion: false, pageEditMode: 'source',
    bookPdf: null, pagePdf: null, currentBookPdf: null, currentPagePdf: null, proofingFocus: null,
    busy: false, processLocked: false, organizerDirty: false, layoutDirty: false, calibrationDirty: false, calibrationDraft: null,
    layoutSaving: false, editLocked: status === 'processing', dirty: true,
    pageDraft: { text: '未保存草稿', page_kind: 'content', cover_fields: [], render_strategy: 'custom_latex', expected_content_revision: 2, expected_layout_revision: 1 },
    api: {}, PAPER_SIZES: { a5: { label: 'A5' } }, useCallback: (callback) => callback,
    errorText: (error) => error.message, sourceLabel: (saved) => saved.source_filename, calibrationFromPage,
    window: { confirm: () => true },
  }
  for (const [setter, key] of [
    ['setDetail', 'detail'], ['setBooks', 'books'], ['setSelectedPageNumber', 'selectedPageNumber'],
    ['setLayoutSaving', 'layoutSaving'], ['setDetailLoading', 'detailLoading'], ['setNotice', 'notice'],
    ['setSaving', 'saving'], ['setDirty', 'dirty'], ['setPageDraft', 'pageDraft'],
    ['setCalibrationDirty', 'calibrationDirty'], ['setCalibrationDraft', 'calibrationDraft'],
    ['setBookPdf', 'bookPdf'], ['setPagePdf', 'pagePdf'], ['setCompiling', 'compiling'],
    ['setProofingFocus', 'proofingFocus'], ['setPageEditMode', 'pageEditMode'], ['setView', 'view'],
    ['setOrganizerDirty', 'organizerDirty'], ['setLayoutDirty', 'layoutDirty'],
  ]) state[setter] = (value) => { state[key] = typeof value === 'function' ? value(state[key]) : value }
  const context = vm.createContext(state)
  for (const name of ['draftFromPage', 'outputDraft']) {
    const start = appSource.indexOf(`function ${name}(`)
    const end = appSource.indexOf('\n}\n', start) + 2
    assert.ok(start >= 0 && end > start, `${name} must exist`)
    vm.runInContext(stripTypeScriptTypes(`${appSource.slice(start, end)}\nglobalThis.${name} = ${name}`), context)
  }
  for (const name of ['invalidatePdfs', 'applyDetail', 'loadDetail', 'applySavedLayout', 'savePaperSize', 'saveLayoutSettings', 'savePage', 'saveCalibration',
    'changePageDraft', 'changeCalibrationDraft', 'compileBook', 'compilePage', 'leaveDrafts', 'choosePage', 'locateDiagnostic']) installCallback(context, appSource, name)
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
  assert.equal(state.books[0].status, 'processing')
  assert.equal(state.detail.book.completed_pages, 0)
  assert.equal(state.layoutSaving, false)
  assert.equal(state.detail.pages[0].status, 'processing')
  assert.equal(state.dirty, true)
})

test('a failed detail refresh after saving layout preserves processing and unsaved page corrections', async () => {
  const state = appState()
  const fresh = completedDetail(state)
  const layout = { source_fidelity_paper: 'source' }
  fresh.book.layout = layout
  fresh.book.render_strategy = 'source_fidelity'
  const draft = state.pageDraft
  state.api.saveBookLayout = async () => fresh.book
  state.api.getBook = async () => { throw new Error('temporary read failure') }
  assert.equal(await state.saveLayoutSettings(layout, 'source_fidelity'), true)
  assert.equal(state.detail.book.layout, layout)
  assert.equal(state.detail.book.render_strategy, 'source_fidelity')
  assert.equal(state.detail.book.status, 'processing')
  assert.equal(state.books[0].status, 'processing')
  assert.equal(state.detail.pages[0].status, 'processing')
  assert.equal(state.layoutSaving, false)
  assert.equal(state.pageDraft, draft)
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

const pdfResult = (url = '/api/books/book/compiled/current.pdf') => ({
  pdf_url: url, warnings: [], diagnostics: [], page_map: [], quality_status: 'unverified',
  generator_version: 'test', diagnostics_version: 'test',
})

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('editing content invalidates the PDF and its diagnostics and rejects a late whole-book response', async () => {
  const state = appState('ready')
  const pending = deferred()
  state.api.compileBook = () => pending.promise
  state.bookPdf = { key: state.bookPdfKey, result: pdfResult(), error: null, origin: 'saved' }
  state.pagePdf = { key: state.pagePdfKey, result: pdfResult(), error: null, origin: 'source_draft' }
  const compilation = state.compileBook()
  state.changePageDraft({ ...state.pageDraft, text: 'new draft' })
  pending.resolve(pdfResult('/old.pdf'))
  await compilation
  assert.equal(state.bookPdf, null)
  assert.equal(state.pagePdf, null)
  assert.equal(state.pageDraft.render_strategy, 'custom_latex')
})

test('an unchanged fidelity-generated complete document keeps structural authority', () => {
  const state = appState('ready')
  state.sourcePage = { ...state.sourcePage, render_strategy: 'source_fidelity', text: '\\documentclass{article}\n\\begin{document}X\\end{document}' }
  const savedDraft = state.draftFromPage(state.sourcePage)
  state.changePageDraft(savedDraft)
  assert.equal(state.outputDraft(state.pageDraft).render_strategy, 'source_fidelity')
  assert.equal(state.outputDraft(state.pageDraft).expected_content_revision, 2)
  assert.equal(state.outputDraft(state.pageDraft).expected_layout_revision, 1)
  state.changePageDraft({ ...savedDraft, text: `${savedDraft.text}\n% edited` })
  assert.equal(state.outputDraft(state.pageDraft).render_strategy, 'custom_latex')
})

test('a page preview started for another book cannot replace the current PDF or diagnostics', async () => {
  const state = appState('ready')
  const pending = deferred()
  state.api.compilePage = () => pending.promise
  const compilation = state.compilePage()
  state.selectedBookIdRef.current = 'other-book'
  state.selectedPageRef.current = 7
  state.previewKeysRef.current.page = 'other-key'
  pending.resolve(pdfResult('/old-book.pdf'))
  await compilation
  assert.equal(state.pagePdf, null)
})

test('a newer page preview survives an older failed request', async () => {
  const state = appState('ready')
  const first = deferred()
  const second = deferred()
  let calls = 0
  state.api.compilePage = () => ++calls === 1 ? first.promise : second.promise
  const older = state.compilePage()
  const newer = state.compilePage()
  second.resolve(pdfResult('/new.pdf'))
  await newer
  first.reject(new Error('old failure'))
  await older
  assert.equal(state.pagePdf.result.pdf_url, '/new.pdf')
  assert.equal(state.pagePdf.error, null)
})

test('saved revision or layout key changes suppress an otherwise successful late compile', async () => {
  const state = appState('ready')
  const pending = deferred()
  state.api.compileBook = () => pending.promise
  const compilation = state.compileBook()
  state.previewKeysRef.current.book = 'new-layout-or-revision'
  pending.resolve(pdfResult('/stale.pdf'))
  await compilation
  assert.equal(state.bookPdf, null)
})

test('choosing another page retains a valid saved whole-book output and invalidates only the page draft result', () => {
  const state = appState('ready')
  state.dirty = false
  const second = { ...state.sourcePage, number: 2, source_page: 8 }
  state.detail.pages.push(second)
  const wholeBook = { key: state.bookPdfKey, result: pdfResult(), origin: 'saved', error: null }
  state.bookPdf = wholeBook
  state.pagePdf = { key: state.pagePdfKey, result: pdfResult('/page.pdf'), origin: 'source_draft', error: null }
  state.choosePage(second)
  assert.equal(state.bookPdf, wholeBook)
  assert.equal(state.pagePdf, null)
  assert.equal(state.selectedPageNumber, 2)
})

const diagnosticFor = (page, changes = {}) => ({
  book_id: 'book', page_number: page.number, source_page: page.source_page,
  content_revision: page.content_revision, layout_revision: page.layout_revision,
  source_bbox: [0.1, 0.2, 0.6, 0.3], output_bbox_bp: [10, 20, 60, 30], output_page_start: 1, line_id: 'line-1',
  ...changes,
})

test('a diagnostic from the current layout draft can use derived revisions while focus binds to the saved source', () => {
  const state = appState('ready')
  const diagnostic = diagnosticFor(state.sourcePage, { content_revision: 3, layout_revision: 2 })
  state.currentPagePdf = { key: state.pagePdfKey, result: { ...pdfResult(), diagnostics: [diagnostic] }, origin: 'layout_draft', error: null }
  state.locateDiagnostic(diagnostic)
  assert.equal(state.view, 'workspace')
  assert.equal(state.proofingFocus.content_revision, 2)
  assert.equal(state.proofingFocus.layout_revision, 1)
  assert.equal(state.proofingFocus.output_page, 1)
})

test('an obsolete layout draft diagnostic cannot locate a newer saved source', () => {
  const state = appState('ready')
  const diagnostic = diagnosticFor(state.sourcePage, { content_revision: 3, layout_revision: 2 })
  state.currentPagePdf = null
  state.locateDiagnostic(diagnostic)
  assert.equal(state.proofingFocus, null)
})

test('whole-book diagnostic navigation keeps its saved output for source comparison', () => {
  const state = appState('ready')
  state.dirty = false
  const second = { ...state.sourcePage, number: 2, source_page: 18 }
  state.detail.pages.push(second)
  const diagnostic = diagnosticFor(second, { output_page_start: 4 })
  const wholeBook = { key: state.bookPdfKey, result: { ...pdfResult(), diagnostics: [diagnostic] }, origin: 'saved', error: null }
  state.bookPdf = wholeBook
  state.locateDiagnostic(diagnostic)
  assert.equal(state.selectedPageNumber, 2)
  assert.equal(state.bookPdf, wholeBook)
  assert.equal(state.proofingFocus.page_number, 2)
  assert.equal(state.proofingFocus.output_page, 4)
})

test('layout draft edits clear both output results without rewriting the source draft', () => {
  const state = appState('ready')
  const sourceDraft = state.pageDraft
  state.bookPdf = { result: pdfResult() }
  state.pagePdf = { result: pdfResult() }
  const calibration = { observation: { schema_version: 1, body_frame: null, lines: [], regions: [], equation_groups: [], review_reasons: [] }, expected_content_revision: 2, expected_layout_revision: 1 }
  state.changeCalibrationDraft(calibration)
  assert.equal(state.pageDraft, sourceDraft)
  assert.equal(state.calibrationDraft, calibration)
  assert.equal(state.calibrationDirty, true)
  assert.equal(state.bookPdf, null)
  assert.equal(state.pagePdf, null)
})

test('manual source saving supplies revisions and does not install a response after a book switch', async () => {
  const state = appState('ready')
  const pending = deferred()
  let submitted
  state.api.savePage = async (_, __, draft) => { submitted = draft; return pending.promise }
  const original = state.detail
  const saving = state.savePage()
  state.selectedBookIdRef.current = 'other-book'
  pending.resolve({ ...state.sourcePage, text: submitted.text, content_revision: 3 })
  await saving
  assert.equal(submitted.expected_content_revision, 2)
  assert.equal(submitted.expected_layout_revision, 1)
  assert.equal(state.detail, original)
  assert.equal(state.dirty, true)
})

test('saving calibration uses its revisions, installs generated source and refreshes book completion', async () => {
  const state = appState('failed')
  const observation = { schema_version: 1, body_frame: null, regions: [], lines: [], equation_groups: [], review_reasons: ['待检查'] }
  const calibration = { observation, expected_content_revision: 2, expected_layout_revision: 1, render_strategy: 'source_fidelity' }
  state.calibrationDraft = calibration
  state.calibrationDirty = true
  const saved = { ...state.sourcePage, status: 'ready', error: null, text: '\\documentclass{article}\n生成源码',
    render_strategy: 'source_fidelity', content_revision: 3, layout_revision: 2, layout_source: observation }
  const fresh = { ...state.detail, book: { ...state.detail.book, status: 'ready', completed_pages: 1, error: null }, pages: [saved] }
  let submitted
  state.api.savePageLayout = async (bookId, pageNumber, draft) => {
    assert.equal(bookId, 'book')
    assert.equal(pageNumber, 1)
    submitted = draft
    return saved
  }
  state.api.getBook = async () => fresh
  await state.saveCalibration()
  assert.equal(submitted, calibration)
  assert.equal(state.pageDraft.text, saved.text)
  assert.equal(state.pageDraft.render_strategy, 'source_fidelity')
  assert.equal(state.pageDraft.expected_content_revision, 3)
  assert.equal(state.calibrationDraft.expected_layout_revision, 2)
  assert.equal(state.calibrationDirty, false)
  assert.equal(state.dirty, false)
  assert.equal(state.detail.book.completed_pages, 1)
  assert.equal(state.books[0].status, 'ready')
})
