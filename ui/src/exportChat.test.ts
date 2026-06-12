import { describe, it, expect, vi, afterEach } from 'vitest'
import { buildExportPayload, exportChat } from './exportChat'
import type { Exchange } from './useChat'

const DONE: Exchange = {
  id: 1,
  prompt: 'What is a Basilisk?',
  status: 'done',
  response: {
    answer: 'A basilisk petrifies with its gaze [1].',
    answerable: true,
    sources: [
      { book: 'mm-5e', chapter: 'Bestiary', section: null, entity: 'Basilisk', page: 24, snippet: 'Stone cold.' },
    ],
  },
}

const REFUSED: Exchange = {
  id: 2,
  prompt: 'What is a warp gate?',
  status: 'done',
  response: { answer: "I couldn't find that.", answerable: false, sources: [] },
}

const ERRORED: Exchange = {
  id: 3,
  prompt: 'What is a beholder?',
  status: 'error',
  error: 'Service unavailable',
}

// ── buildExportPayload (pure — no mocks needed) ──────────────────────────────

describe('buildExportPayload', () => {
  it('has an exported timestamp at root', () => {
    const p = buildExportPayload([DONE])
    expect(p.exported).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })

  it('maps a done exchange with answer + sources', () => {
    const [ex] = buildExportPayload([DONE]).exchanges
    expect(ex.prompt).toBe('What is a Basilisk?')
    expect(ex.status).toBe('done')
    expect(ex.answer).toBe('A basilisk petrifies with its gaze [1].')
    expect(ex.answerable).toBe(true)
    expect(ex.sources).toHaveLength(1)
    expect(ex.error).toBeNull()
  })

  it('maps a refused exchange (answerable=false, empty sources)', () => {
    const [ex] = buildExportPayload([REFUSED]).exchanges
    expect(ex.answerable).toBe(false)
    expect(ex.sources).toHaveLength(0)
    expect(ex.error).toBeNull()
  })

  it('maps an error exchange (null answer, non-null error)', () => {
    const [ex] = buildExportPayload([ERRORED]).exchanges
    expect(ex.answer).toBeNull()
    expect(ex.answerable).toBeNull()
    expect(ex.error).toBe('Service unavailable')
  })

  it('preserves exchange order', () => {
    const { exchanges } = buildExportPayload([DONE, REFUSED, ERRORED])
    expect(exchanges.map((e) => e.prompt)).toEqual([
      'What is a Basilisk?',
      'What is a warp gate?',
      'What is a beholder?',
    ])
  })
})

// ── exportChat (side-effects — mocked) ───────────────────────────────────────

function mockBrowserDownload() {
  const anchor = { href: '', download: '', click: vi.fn() }
  vi.spyOn(document, 'createElement').mockReturnValue(anchor as unknown as HTMLAnchorElement)
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  return anchor
}

describe('exportChat', () => {
  afterEach(() => vi.restoreAllMocks())

  it('triggers a click (initiates download)', () => {
    const anchor = mockBrowserDownload()
    exportChat([DONE])
    expect(anchor.click).toHaveBeenCalledOnce()
  })

  it('sets a timestamped dnd-chat-*.json filename', () => {
    const anchor = mockBrowserDownload()
    exportChat([DONE])
    expect(anchor.download).toMatch(/^dnd-chat-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.json$/)
  })

  it('revokes the object URL after clicking', () => {
    mockBrowserDownload()
    exportChat([DONE])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock')
  })
})
