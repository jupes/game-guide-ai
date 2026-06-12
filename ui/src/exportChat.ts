import type { Exchange } from './useChat'

/** Pure serializer — easy to unit-test without mocking browser APIs. */
export function buildExportPayload(exchanges: Exchange[]) {
  return {
    exported: new Date().toISOString(),
    exchanges: exchanges.map((e) => ({
      prompt: e.prompt,
      status: e.status,
      answer: e.response?.answer ?? null,
      answerable: e.response?.answerable ?? null,
      sources: e.response?.sources ?? [],
      error: e.error ?? null,
    })),
  }
}

/** Triggers a browser file-download with the full conversation as JSON. */
export function exportChat(exchanges: Exchange[]): void {
  const json = JSON.stringify(buildExportPayload(exchanges), null, 2)
  const blob = new Blob([json], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  a.href = url
  a.download = `dnd-chat-${ts}.json`
  a.click()
  URL.revokeObjectURL(url)
}
