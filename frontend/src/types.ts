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
export type RenderStrategy = 'source_fidelity' | 'legacy_template' | 'custom_latex'
export type FontFamily = 'songti' | 'heiti' | 'kaiti'
export type EvidenceBasis = 'file_metadata' | 'local_measurement' | 'model_estimate' | 'manual' | 'project'
export type BBox = [number, number, number, number]
export type BoxBp = [number, number, number, number]
export type AffineTransform = [number, number, number, number, number, number]

export type LayoutSettings = {
  source_fidelity_paper: 'project' | 'source'
  font_family: FontFamily
  font_size_pt: number | null
  line_height: number
  paragraph_indent: number
  paragraph_spacing_pt: number
  margin_mm: number | null
}

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
  content_format: 'latex'
  render_strategy: RenderStrategy
  layout: LayoutSettings
}

export type Page = {
  number: number
  source_id: string
  source_filename: string
  source_page: number
  status: Status
  error: string | null
  text: string
  render_strategy: RenderStrategy
  content_revision: number
  layout_revision: number
  generated_content_revision: number | null
  layout_source: SourceFidelityLayout | null
  source_metadata: PageSourceMetadata | null
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

export type PageDraft = Pick<Page, 'text' | 'page_kind' | 'cover_fields'> & {
  render_strategy?: RenderStrategy
  expected_content_revision?: number
  expected_layout_revision?: number
}

export type LineStyle = {
  font_family: FontFamily | null
  font_size_bp: number | null
  font_size_ratio: number | null
  bold: boolean | null
  italic: boolean | null
  basis: EvidenceBasis | null
}

export type LayoutRegion = {
  region_id: string
  kind: 'body' | 'header' | 'footer' | 'column' | 'paragraph' | 'equation' | 'table' | 'figure' | 'footnote' | 'other'
  order: number
  bbox: BBox | null
  parent_id: string | null
  basis: EvidenceBasis | null
}

export type LayoutLine = {
  line_id: string
  block_id: string
  order: number
  kind: 'text' | 'equation' | 'header' | 'footer' | 'page_number' | 'table' | 'footnote' | 'caption'
  latex: string
  bbox: BBox | null
  baseline: number | null
  style: LineStyle
  basis: EvidenceBasis | null
}

export type EquationNumber = {
  latex: string
  line_id: string
  bbox: BBox | null
  anchor_x: number | null // Right edge of the printed number in canonical coordinates.
}

export type EquationGroup = {
  group_id: string
  line_ids: string[]
  bbox: BBox | null
  align_x: number | null
  number: EquationNumber | null
  basis: EvidenceBasis | null
}

export type LayoutObservation = {
  schema_version: 1
  body_frame: BBox | null
  regions: LayoutRegion[]
  lines: LayoutLine[]
  equation_groups: EquationGroup[]
  review_reasons: string[]
}

export type PdfSourceGeometry = {
  media_box_bp: BoxBp
  crop_box_bp: BoxBp
  rotation: 0 | 90 | 180 | 270
  width_bp: number // Unrotated crop width.
  height_bp: number // Unrotated crop height.
}

export type PageSourceMetadata = {
  book_id: string
  page_number: number
  source_id: string
  source_page: number
  source_kind: 'pdf' | 'image'
  source_file_fingerprint: string
  image_fingerprint: string
  canonical_width_px: number
  canonical_height_px: number
  source_width_px: number | null
  source_height_px: number | null
  source_coordinate_space: 'original_image_px' | 'unrotated_crop_bp'
  canonical_to_source_affine: AffineTransform
  pdf_geometry: PdfSourceGeometry | null
}

export type SourceFidelityLayout = LayoutObservation & {
  source: PageSourceMetadata
  content_revision: number
  layout_revision: number
  generated_content_revision: number | null
  generator_version: string | null
  canvas_width_bp: number | null
  canvas_height_bp: number | null
  canvas_basis: 'file_metadata' | 'manual' | 'project' | null
  body_font_size_bp: number | null
  body_font_family: FontFamily | null
  body_font_basis: EvidenceBasis | null
}

export type LayoutCalibrationUpdate = {
  observation: LayoutObservation
  expected_content_revision: number
  expected_layout_revision: number
  canvas_width_bp?: number | null
  canvas_height_bp?: number | null
  body_font_size_bp?: number | null
  body_font_family?: FontFamily | null
  render_strategy?: RenderStrategy
}

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
export type QualityStatus = 'passed' | 'needs_review' | 'unverified' | 'compile_failed'

export type RenderDiagnostic = {
  code: string
  severity: 'error' | 'warning' | 'info'
  message: string
  suggestion: string
  basis: string
  book_id: string | null
  page_number: number
  source_id: string
  source_page: number
  arrangement_position: number
  output_page_start: number | null
  output_page_end: number | null
  line_id: string | null
  block_id: string | null
  source_bbox: BBox | null
  output_bbox_bp: BoxBp | null
  overflow_bp: number | null
  content_revision: number
  layout_revision: number
  coverage: 'complete' | 'partial' | 'none'
}

export type PageMapEntry = {
  page_number: number
  source_id: string
  source_page: number
  arrangement_position: number
  output_page_start: number
  output_page_end: number
  content_revision: number
  layout_revision: number
  render_strategy: RenderStrategy
  output_width_bp: number | null
  output_height_bp: number | null
  source_to_output_affine: AffineTransform | null
  canvas_scale: number | null
}

export type PdfCompileResult = {
  pdf_url: string | null
  warnings: string[]
  diagnostics: RenderDiagnostic[]
  page_map: PageMapEntry[]
  quality_status: QualityStatus
  generator_version: string | null
  diagnostics_version: string | null
}
export type Arrangement = { book: Book; files: SourceFile[]; pages: Page[]; order: number[] }
export type Notice = { kind: 'success' | 'error' | 'info'; text: string } | null
