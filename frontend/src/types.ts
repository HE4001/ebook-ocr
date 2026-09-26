export type Usage = {
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  complete: boolean
}

export type ApiProtocol = 'openai_responses' | 'gemini'

export type Settings = {
  api_protocol: ApiProtocol
  base_url: string
  models_path: string
  responses_path: string
  extraction_model: string
  reasoning_effort: string
  classification_model: string // Kept for existing backend settings; no longer used by the page agent.
  api_key?: string
  has_api_key?: boolean
  structured_output: boolean
  timeout_seconds: number
  processing_concurrency: number
  context_reuse_enabled: boolean
  context_reuse_max_pages: number
}

export type Status = 'uploaded' | 'processing' | 'pausing' | 'paused' | 'ready' | 'failed' | 'interrupted'

export type PaperSize = 'a4' | 'a5' | 'a6' | 'b5' | 'b6' | 'trade_6x9'

export type Book = {
  id: string
  title: string
  filename: string
  status: Status
  page_count: number
  file_count: number
  upload_confirmed: boolean
  selection_confirmed: boolean
  selected_page_count: number
  completed_pages: number
  error: string | null
  created_at: string
  usage: Usage
  paper_size: PaperSize
}

export type Page = {
  number: number
  source_id: string
  source_filename: string
  source_page: number
  status: Status
  error: string | null
  text: string
  page_kind: PageKind
  page_side: PageSide
  cover_fields: CoverField[]
  header_segments: MarginSegment[]
  footer_segments: MarginSegment[]
  usage: Usage
  attempts: number
}

export type PageKind = 'content' | 'front_cover' | 'back_cover'
export type PageSide = 'left' | 'right' | 'unknown'

export type CoverField = {
  kind: 'title' | 'subtitle' | 'author' | 'translator' | 'editor' | 'publisher' | 'series' | 'edition' | 'publication_year' | 'isbn'
  text: string
}

export type PageDraft = Pick<Page, 'text' | 'page_kind' | 'cover_fields'>

export type MarginSegment = {
  kind: 'text' | 'page_number'
  text: string
  alignment: 'left' | 'center' | 'right'
  row: number
  font_size: 'small' | 'normal'
  bold: boolean
  italic: boolean
}

export type SourceFile = {
  id: string
  filename: string
  kind: 'pdf' | 'image'
  page_count: number
  position: number
  parent_id: string | null
}

export type BookDetail = { book: Book; pages: Page[]; files: SourceFile[] }
export type Arrangement = { book: Book; files: SourceFile[]; pages: Page[]; order: number[] }
export type Notice = { kind: 'success' | 'error' | 'info'; text: string } | null
