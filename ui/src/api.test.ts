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
    const result = await postChat('What is a Basilisk?', fakeFetch(200, GROUNDED))
    expect(result).toEqual({ kind: 'ok', response: GROUNDED })
  })

  it('returns ok for a refusal (200, answerable=false) — not an error', async () => {
    const result = await postChat('Pokemon?', fakeFetch(200, REFUSAL))
    expect(result.kind).toBe('ok')
    if (result.kind === 'ok') expect(result.response.answerable).toBe(false)
  })

  it('maps 422 to a validation error message', async () => {
    const result = await postChat('', fakeFetch(422, { detail: 'invalid' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/prompt/i)
  })

  it('maps 503 to a service-unavailable message', async () => {
    const result = await postChat('Q', fakeFetch(503, { detail: 'service not ready' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unavailable|not ready/i)
  })

  it('maps a network failure to an error result', async () => {
    const boom: typeof fetch = async () => {
      throw new TypeError('Failed to fetch')
    }
    const result = await postChat('Q', boom)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })

  it('POSTs the prompt as JSON to /chat', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(url), init }
      return new Response(JSON.stringify(GROUNDED), { status: 200 })
    }) as typeof fetch
    await postChat('What is a Basilisk?', spy)
    expect(captured).not.toBeNull()
    expect(captured!.url).toBe('/chat')
    expect(captured!.init?.method).toBe('POST')
    expect(JSON.parse(String(captured!.init?.body))).toEqual({ prompt: 'What is a Basilisk?' })
  })
})
