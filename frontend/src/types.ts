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
  arrangement_revision: number
  output_settings_version: number
}

export type Page = {
  number: number
  page_id: string
  source_version: number
  current_revision_id: string | null
  result_status: ResultStatus | null
  manual_protected: boolean
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
  page_id: string
  source_id: string
  source_file_id: string
  source_page: number
  source_version: number
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
  source_assets: SourceRegionAsset[]
  source_disposition: SourceDisposition
  disposition_reason: string | null
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
export type SourcePageSummary = {
  page_id: string
  source_version: number
  number: number
  source_id: string
  source_page: number
  source_filename: string
  width: number
  height: number
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

export type SourceDisposition = 'transcribed' | 'regions_preserved' | 'source_page_preserved'
export type SourceRegionAsset = {
  asset_id: string
  region_id: string | null
  bbox: BBox
  image_name: string
  purpose: 'figure' | 'uncertain_content' | 'source_page'
  reason: string
}
export type WorkflowStage = 'prepare' | 'recognize' | 'review' | 'recover' | 'layout' | 'render' | 'export' | 'verify' | 'repair' | 'finalize'
export type ExecutionStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted' | 'finished'
export type RunStatus = 'queued' | 'running' | 'pausing' | 'paused' | 'succeeded' | 'failed' | 'interrupted' | 'finished'
export type ResultStatus = 'auto_passed' | 'completed_with_issues' | 'failed'
export type CheckStatus = 'passed' | 'uncertain' | 'unverified' | 'failed'
export type RunPolicy = {
  replace_page_ids: string[]
  requests_per_page?: number
  page_request_limit?: number
  temporary_retry_limit?: number
  local_recovery_limit?: number
  compile_limit?: number
  response_body_limit_bytes?: number
}
export type RunCreate = {
  page_ids?: string[]
  pages?: number[]
  expected_arrangement_revision?: number
  selection_revision?: number
  continuation_run_id?: string
  policy?: RunPolicy
  request_limit?: number
  client_request_id: string
}
export type PageTask = {
  run_id: string
  page_id: string
  page_number: number
  source_version: number
  position: number
  base_revision_id: string | null
  base_content_revision: number
  base_layout_revision: number
  stage: WorkflowStage
  state: ExecutionStatus
  completed_stages: WorkflowStage[]
  candidate_revision_id: string | null
  stage_data: Record<string, unknown>
  result_status: ResultStatus | null
  error: string | null
  request_count: number
  retry_count: number
  recognize_count: number
  review_count: number
  repair_count: number
  compile_count: number
  recovery_round_count: number
  outcome: PageOutcome | null
  errors: WorkflowError[]
}
export type RunCounts = {
  total: number
  completed: number
  auto_passed: number
  completed_with_issues: number
  failed: number
  editable: number
  regions_preserved: number
  page_preserved: number
  no_result: number
  protected_existing: number
}
export type Run = {
  workflow_version: 1 | 2
  run_id: string
  book_id: string
  arrangement_revision: number
  page_ids: string[]
  settings_snapshot: Record<string, unknown>
  policy: RunPolicy
  status: RunStatus
  request_limit: number
  request_count: number
  generator_version: string
  created_at: string
  updated_at: string
  error: string | null
  usage: Usage
  counts: RunCounts
  tasks: PageTask[]
  export_manifest_id: string | null
  selection_revision: number | null
  continuation_run_id: string | null
  output_snapshot_id: string | null
  continuation_outputs_needed: boolean
}
export type Revision = {
  workflow_version: 1 | 2
  page_content: PageContent | null
  page_layout: PageLayout | null
  revision_id: string
  parent_revision_id: string | null
  book_id: string
  page_id: string
  page_number: number
  source_version: number
  content_revision: number
  layout_revision: number
  origin: 'legacy' | 'manual' | 'automatic'
  run_id: string | null
  text: string
  render_strategy: RenderStrategy
  layout_source: SourceFidelityLayout | null
  source_metadata: PageSourceMetadata | null
  page_kind: PageKind
  page_side: PageSide
  cover_fields: CoverField[]
  header_segments: MarginSegment[]
  footer_segments: MarginSegment[]
  generated_content_revision: number | null
  generator_version: string | null
  created_at: string
}
export type Issue = {
  issue_id: string
  run_id: string
  page_id: string
  revision_id: string
  category: string
  severity: 'error' | 'warning' | 'info'
  region_id: string | null
  line_id: string | null
  source_bbox: BBox | null
  reason: string
  disposition: string
}
export type Assessment = {
  assessment_id: string
  revision_id: string
  run_id: string
  page_id: string
  content: CheckStatus
  layout: CheckStatus
  coverage: CheckStatus
  rule_version: string
  issues: Issue[]
  created_at: string
}
export type Attempt = {
  attempt_id: string
  run_id: string
  page_id: string
  stage: WorkflowStage
  ordinal: number
  retry: boolean
  state: 'reserved' | 'succeeded' | 'failed' | 'unknown' | 'cancelled'
  usage: Usage
  provider_request_id: string | null
  error: string | null
  created_at: string
  finished_at: string | null
  purpose: 'basic_recognition' | 'basic_review' | 'local_recognition' | 'recovery' | 'recovery_review' | 'retry' | null
  reservation_key: string | null
  recovery_round: number | null
  block_ids: string[]
  sent_at: string | null
  settled_at: string | null
  retryable: boolean
  retry_of_attempt_id?: string | null
}
export type ExportManifestPage = {
  page_id: string
  page_number: number
  position: number
  revision_id: string
  source_file_id: string
  source_page: number
  source_version: number
  source_filename: string
  image_name: string
  result_status: ResultStatus | null
  assessment: Assessment | null
}
export type ExportManifest = {
  manifest_id: string
  book_id: string
  run_id: string | null
  arrangement_revision: number
  output_settings_version: number
  settings_snapshot: Record<string, unknown>
  generator_version: string
  pages: ExportManifestPage[]
  complete: boolean
  issues: Issue[]
  created_at: string
  outputs: Record<string, string>
}
export type ExportManifestCreate = {
  expected_arrangement_revision: number
  page_ids?: string[]
  run_id?: string
}
export type PageResult = {
  page_id: string
  page_number: number
  current_revision_id: string | null
  result_status: ResultStatus | null
  content: CheckStatus
  layout: CheckStatus
  coverage: CheckStatus
  source_disposition: SourceDisposition | null
  issues: Issue[]
}

export type SelectionDraft = {
  book_id: string
  selection_revision: number
  page_ids: string[]
  source_versions: Record<string, number>
  valid: boolean
}
export type SelectionUpdate = {
  page_ids: string[]
  expected_selection_revision: number
  source_versions: Record<string, number>
}
export type ContentConclusion = 'usable' | 'uncertain' | 'unavailable' | 'unverified'
export type LayoutConclusion = 'faithful' | 'approximate' | 'unavailable' | 'unverified'
export type ContentRole = 'body' | 'header' | 'footer' | 'title' | 'footnote' | 'caption' | 'page_number' | 'other'
export type RecoveryReason = 'small_text' | 'reading_order' | 'truncated_response' | 'invalid_structure' | 'missing_content' | 'equation' | 'table' | 'unreadable_source' | 'budget' | 'service_configuration' | 'temporary_service' | 'unknown_consumption' | 'layout' | 'render'
export type InlineSpan = { kind: 'text'; text: string; bold: boolean; italic: boolean; font_family: FontFamily | null } | { kind: 'math'; text: string }
export type VisualLine = { line_id: string; spans: InlineSpan[]; bbox: BBox | null; paragraph_start: boolean }
export type EquationContentLine = { line_id: string; latex: string; number: string | null; alignment: 'left' | 'center' | 'right' | 'aligned' | 'unknown'; bbox: BBox | null }
export type BlockBase = {
  block_id: string
  role: ContentRole
  bbox: BBox | null
  source_region_id: string | null
  crop_id: string | null
  content_revision_id: string
  response_id: string | null
  response_index: number | null
  recognition_status: ContentConclusion
  review_status: ContentConclusion
  unresolved_reasons: string[]
  uncertainty: string[]
}
export type TextBlock = BlockBase & { type: 'text'; lines: VisualLine[] }
export type EquationBlock = BlockBase & { type: 'equation'; lines: EquationContentLine[] }
export type TableCell = { cell_id: string; row: number; column: number; row_span: number; column_span: number; lines: VisualLine[]; bbox: BBox | null; preserved: boolean; reason: string | null }
export type TableBlock = BlockBase & { type: 'table'; rows: number; columns: number; cells: TableCell[] }
export type FigureBlock = BlockBase & { type: 'figure'; description: string | null; caption_block_ids: string[] }
export type ContentBlock = TextBlock | EquationBlock | TableBlock | FigureBlock
export type ContentIssue = {
  issue_id: string
  category: RecoveryReason
  reason: string
  block_id: string | null
  source_bbox: BBox | null
  response_id: string | null
  response_index: number | null
  field_path: string | null
  severity: 'error' | 'warning' | 'info'
  resolved: boolean
}
export type PageContent = {
  schema_version: 2
  content_revision_id: string
  book_id: string
  page_id: string
  source_version: number
  page_kind: 'content' | 'front_cover' | 'back_cover' | 'contents' | 'blank' | 'other'
  blank: boolean
  blocks: ContentBlock[]
  issues: ContentIssue[]
  response_ids: string[]
}
export type BlockParseFailure = { response_index: number | null; field_path: string; reason: string; source_bbox: BBox | null; category: RecoveryReason }
export type ContentParseResult = { content: PageContent | null; failures: BlockParseFailure[]; json_complete: boolean }
export type CoarseRegion = {
  region_id: string
  kind: 'text' | 'body' | 'column' | 'equation' | 'table' | 'figure' | 'header' | 'footer' | 'title' | 'spanning_title' | 'footnote' | 'margin' | 'unknown'
  bbox: BBox
  order: number
  column_id: string | null
  basis: EvidenceBasis
  reasons: string[]
}
export type CropMapping = {
  crop_id: string
  page_id: string
  source_version: number
  bbox: BBox
  width_px: number
  height_px: number
  crop_to_canonical_affine: AffineTransform // Normalized crop to normalized canonical page.
  source_region_ids: string[]
  reason: string
}
export type RecognitionInput = { image_path: string; kind: 'overview' | 'crop'; mapping: CropMapping | null }
export type PageLinePlacement = { line_id: string; block_id: string; order: number; bbox: BBox | null; baseline: number | null; style: LineStyle; basis: EvidenceBasis | null }
export type PageLayout = {
  schema_version: 2
  layout_revision_id: string
  content_revision_id: string
  page_id: string
  source_version: number
  source: PageSourceMetadata
  canvas_width_bp: number | null
  canvas_height_bp: number | null
  canvas_basis: 'file_metadata' | 'manual' | 'project' | null
  body_frame: BBox | null
  regions: LayoutRegion[]
  lines: PageLinePlacement[]
  equation_groups: EquationGroup[]
  body_font_size_bp: number | null
  body_font_family: FontFamily | null
  body_font_basis: EvidenceBasis | null
  source_assets: SourceRegionAsset[]
  conclusion: LayoutConclusion
  review_reasons: string[]
}
export type WorkflowError = {
  stage: WorkflowStage
  category: RecoveryReason
  message: string
  field_path: string | null
  attempt_id: string | null
  block_id: string | null
  source_bbox: BBox | null
  phase: 'initial' | 'recovery' | 'output'
}
export type PageOutcome = {
  page_id: string
  source_version: number
  content: ContentConclusion
  layout: LayoutConclusion
  source_disposition: 'transcribed' | 'regions_preserved' | 'page_preserved'
  content_revision_id: string | null
  layout_revision_id: string | null
  adopted_revision_id: string | null
  source_readable: boolean
  protected_existing: boolean
  errors: WorkflowError[]
}
export type PageOutcomeSummary = { page_id: string; page_number: number; position: number; stage: WorkflowStage; state: ExecutionStatus; outcome: PageOutcome | null }
export type RecognitionResponse = {
  attempt_id: string
  run_id: string
  page_id: string
  body_asset: string | null
  body_storage: 'saved' | 'failed'
  body_bytes: number
  body_truncated: boolean
  completion_status: 'complete' | 'truncated' | 'incomplete' | 'unknown'
  usage: Usage
  provider_request_id: string | null
  parse_errors: WorkflowError[]
  storage_error: string | null
  created_at: string
}
export type OutputFormat = { status: 'pending' | 'generating' | 'available' | 'failed'; asset: string | null; error: string | null }
export type OutputSnapshotPage = {
  page_id: string
  position: number
  source_version: number
  source_file_id: string
  source_page: number
  source_filename: string
  image_name: string
  source_metadata: PageSourceMetadata | null
  revision_id: string | null
  content_revision_id: string | null
  layout_revision_id: string | null
  outcome: PageOutcome
}
export type OutputSnapshot = {
  workflow_version: 2
  output_snapshot_id: string
  book_id: string
  run_id: string
  selection_revision: number
  page_ids: string[]
  output_settings_version: number
  settings_snapshot: Record<string, unknown>
  generator_version: string
  pages: OutputSnapshotPage[]
  formats: Record<'json' | 'pdf' | 'latex', OutputFormat>
  created_at: string
}
export type RunSummary = {
  workflow_version: 1 | 2
  run_id: string
  book_id: string
  selection_revision: number | null
  status: RunStatus
  counts: RunCounts
  active_stages: Record<string, number>
  updated_at: string
  error: string | null
  recent_errors: WorkflowError[]
  output_snapshot_id: string | null
  formats: Record<string, OutputFormat>
  request_limit: number
  request_count: number
  usage: Usage
  model_wait_started_at: string | null
}
