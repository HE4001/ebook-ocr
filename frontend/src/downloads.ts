export function downloadText(filename: string, content: string, type: string): void {
  downloadBlob(filename, new Blob([content], { type }))
}

export function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  downloadUrl(filename, url)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadUrl(filename: string, url: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
}

export function safeFilename(value: string): string {
  return value.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').trim() || '电子书'
}
