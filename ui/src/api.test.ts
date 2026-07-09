import { describe, it, expect } from 'vitest'
import { postChat } from './api'
import type { ChatResponse } from './api'

const GROUNDED: ChatResponse = {
  answer: 'A basilisk petrifies with its gaze [1].',
  sources: [
    {
      book: 'mm-5e', chapter: 'Bestiary', section: 'Stat Block',
      entity: 'Basilisk', page: 12, snippet: 'Armor Class 15 ...',
    },
  ],
  answerable: true,
}

const REFUSAL: ChatResponse = {
  answer: "I couldn't find that in the D&D 5e sources I have.",
  sources: [],
  answerable: false,
}

function fakeFetch(status: number, body?: unknown): typeof fetch {
  return (async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })) as typeof fetch
}

describe('postChat', () => {
  it('returns ok with the grounded response on 200', async () => {
    const result = await postChat('What is a Basilisk?', 'sage', null, fakeFetch(200, GROUNDED))
    expect(result).toEqual({ kind: 'ok', response: GROUNDED })
  })

  it('returns ok for a refusal (200, answerable=false) — not an error', async () => {
    const result = await postChat('Pokemon?', 'sage', null, fakeFetch(200, REFUSAL))
    expect(result.kind).toBe('ok')
    if (result.kind === 'ok') expect(result.response.answerable).toBe(false)
  })

  it('maps 422 to a validation error message', async () => {
    const result = await postChat('', 'sage', null, fakeFetch(422, { detail: 'invalid' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/prompt/i)
  })

  it('maps 503 to a service-unavailable message', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetch(503, { detail: 'service not ready' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unavailable|not ready/i)
  })

  it('maps a network failure to an error result', async () => {
    const boom: typeof fetch = async () => {
      throw new TypeError('Failed to fetch')
    }
    const result = await postChat('Q', 'sage', null, boom)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })

  it('POSTs the prompt as JSON to /chat with mode and conversation_id', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(url), init }
      return new Response(JSON.stringify(GROUNDED), { status: 200 })
    }) as typeof fetch
    await postChat('What is a Basilisk?', 'sage', null, spy)
    expect(captured).not.toBeNull()
    expect(captured!.url).toBe('/chat')
    expect(captured!.init?.method).toBe('POST')
    expect(JSON.parse(String(captured!.init?.body))).toEqual({
      prompt: 'What is a Basilisk?',
      mode: 'sage',
      conversation_id: null,
    })
  })

  it('sends the correct mode in the request body', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(url), init }
      return new Response(JSON.stringify(GROUNDED), { status: 200 })
    }) as typeof fetch
    await postChat('Cast fireball', 'spell', null, spy)
    expect(JSON.parse(String(captured!.init?.body))).toMatchObject({ mode: 'spell' })
  })

  it('sends conversationId as conversation_id in the request body', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(url), init }
      return new Response(JSON.stringify(GROUNDED), { status: 200 })
    }) as typeof fetch
    await postChat('Q', 'sage', 'conv-abc', spy)
    expect(JSON.parse(String(captured!.init?.body))).toMatchObject({ conversation_id: 'conv-abc' })
  })
})

// ── channel-chats CP-B — getMessages ──────────────────────────────────────────

import { getMessages } from './api'
import type { StoredMessage } from './api'

const STORED: StoredMessage[] = [
  { id: 1, role: 'user', content: 'What is a goblin?', mode: 'sage', created_at: '2026-07-08T12:00:00Z' },
  { id: 2, role: 'assistant', content: 'A small green menace.', mode: 'sage', created_at: '2026-07-08T12:00:01Z' },
]

describe('getMessages', () => {
  it('returns ok with the stored messages on 200', async () => {
    const result = await getMessages(
      'conv-1',
      fakeFetch(200, { conversation_id: 'conv-1', messages: STORED }),
    )
    expect(result).toEqual({ kind: 'ok', messages: STORED })
  })

  it('GETs /conversations/{id}/messages with the id URL-encoded', async () => {
    let captured: string | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL) => {
      captured = String(url)
      return new Response(JSON.stringify({ conversation_id: 'a/b', messages: [] }), { status: 200 })
    }) as typeof fetch
    await getMessages('a/b', spy)
    expect(captured).toBe('/conversations/a%2Fb/messages')
  })

  it('maps a 503 to an error result', async () => {
    const result = await getMessages('conv-1', fakeFetch(503))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/503/)
  })

  it('maps a network failure to an error result', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    const result = await getMessages('conv-1', failing)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })
})
