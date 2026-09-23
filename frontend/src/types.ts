export type Usage = {
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  complete: boolean
}

export type Settings = {
  base_url: string
  responses_path: string
  extraction_model: string
  reasoning_effort: string
  classification_model: string // Kept for existing backend settings; no longer used by the page agent.
  api_key?: string
  has_api_key?: boolean
  structured_output: boolean
  max_output_tokens: number
  timeout_seconds: number
}

export type Status = 'uploaded' | 'processing' | 'pausing' | 'paused' | 'ready' | 'failed' | 'interrupted'

export type Book = {
  id: string
  title: string
  filename: string
  status: Status
  page_count: number
  completed_pages: number
  error: string | null
  created_at: string
  usage: Usage
}

export type Page = {
  number: number
  status: Status
  error: string | null
  text: string
  usage: Usage
  attempts: number
}

export type BookDetail = { book: Book; pages: Page[] }
export type Notice = { kind: 'success' | 'error' | 'info'; text: string } | null
