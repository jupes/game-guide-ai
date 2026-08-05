import { describe, it, expect } from 'vitest'
import { postChat, signup, login, logout, getMe } from './api'
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

/** A 200 whose body is HTML, not JSON — what a misrouted proxy serves (cnqf). */
function htmlFetch(): typeof fetch {
  return (async () =>
    new Response('<!doctype html><html><body>SPA</body></html>', {
      status: 200,
      headers: { 'Content-Type': 'text/html' },
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

  it('maps a 200 with a non-JSON body to an error result, not a throw', async () => {
    const result = await postChat('Q', 'sage', null, htmlFetch())
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unreadable/i)
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

  it('maps a 200 with a non-JSON body to an error result, not a throw', async () => {
    const result = await getMessages('conv-1', htmlFetch())
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unreadable/i)
  })
})

// ── swe1.6 — file attachments ─────────────────────────────────────────────────

import { uploadAttachment, getAttachments } from './api'
import type { Attachment } from './api'

const ATTACHMENT: Attachment = {
  id: 1, filename: 'notes.txt', content_type: 'text/plain', chars: 42,
  created_at: '2026-07-20T12:00:00Z',
}

function fakeFile(content: string, name: string, type: string): File {
  return new File([content], name, { type })
}

describe('uploadAttachment', () => {
  it('returns ok with the stored attachment on 200', async () => {
    const result = await uploadAttachment(
      'conv-1', fakeFile('the orb is cursed', 'notes.txt', 'text/plain'),
      fakeFetch(200, { conversation_id: 'conv-1', attachment: ATTACHMENT }),
    )
    expect(result).toEqual({ kind: 'ok', attachment: ATTACHMENT })
  })

  it('maps a 200 with a non-JSON body to an error result, not a throw', async () => {
    const result = await uploadAttachment(
      'conv-1', fakeFile('x', 'notes.txt', 'text/plain'), htmlFetch(),
    )
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unreadable/i)
  })

  it('base64-encodes a multi-chunk (>32 KiB) file losslessly', async () => {
    // Exercises the 32 KiB slice boundary in fileToBase64: byte values that
    // span the full 0-255 range across several chunks must round-trip exactly.
    const size = 100_000
    const bytes = new Uint8Array(size)
    for (let i = 0; i < size; i++) bytes[i] = i % 256
    let sent: string | null = null
    const spy: typeof fetch = (async (_url: RequestInfo | URL, init?: RequestInit) => {
      sent = (JSON.parse(String(init?.body)) as { data: string }).data
      return new Response(
        JSON.stringify({ conversation_id: 'c', attachment: ATTACHMENT }),
        { status: 200 },
      )
    }) as typeof fetch
    await uploadAttachment('c', new File([bytes], 'big.bin.txt', { type: 'text/plain' }), spy)
    expect(sent).not.toBeNull()
    const decoded = atob(sent!)
    expect(decoded.length).toBe(size)
    for (const probe of [0, 0x7fff, 0x8000, 0x8001, size - 1]) {
      expect(decoded.charCodeAt(probe)).toBe(probe % 256)
    }
  })

  it('POSTs base64-encoded file content as JSON to /conversations/{id}/attachments', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      captured = { url: String(url), init }
      return new Response(
        JSON.stringify({ conversation_id: 'conv-1', attachment: ATTACHMENT }), { status: 200 },
      )
    }) as typeof fetch
    await uploadAttachment('conv-1', fakeFile('hello', 'notes.txt', 'text/plain'), spy)
    expect(captured).not.toBeNull()
    expect(captured!.url).toBe('/conversations/conv-1/attachments')
    expect(captured!.init?.method).toBe('POST')
    const body = JSON.parse(String(captured!.init?.body)) as {
      filename: string; content_type: string; data: string
    }
    expect(body.filename).toBe('notes.txt')
    expect(body.content_type).toBe('text/plain')
    expect(atob(body.data)).toBe('hello')
  })

  it('maps 415 to an unsupported-type error message', async () => {
    const result = await uploadAttachment(
      'conv-1', fakeFile('x', 'art.png', 'image/png'), fakeFetch(415, { detail: 'nope' }),
    )
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/type|support/i)
  })

  it('maps 413 to a too-large error message', async () => {
    const result = await uploadAttachment(
      'conv-1', fakeFile('x', 'big.txt', 'text/plain'), fakeFetch(413, { detail: 'too big' }),
    )
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/large|size|limit/i)
  })

  it('maps a network failure to an error result', async () => {
    const boom: typeof fetch = async () => {
      throw new TypeError('Failed to fetch')
    }
    const result = await uploadAttachment('conv-1', fakeFile('x', 'a.txt', 'text/plain'), boom)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })
})

describe('getAttachments', () => {
  it('returns ok with the stored attachments on 200', async () => {
    const result = await getAttachments(
      'conv-1', fakeFetch(200, { conversation_id: 'conv-1', attachments: [ATTACHMENT] }),
    )
    expect(result).toEqual({ kind: 'ok', attachments: [ATTACHMENT] })
  })

  it('GETs /conversations/{id}/attachments with the id URL-encoded', async () => {
    let captured: string | null = null
    const spy: typeof fetch = (async (url: RequestInfo | URL) => {
      captured = String(url)
      return new Response(JSON.stringify({ conversation_id: 'a/b', attachments: [] }), { status: 200 })
    }) as typeof fetch
    await getAttachments('a/b', spy)
    expect(captured).toBe('/conversations/a%2Fb/attachments')
  })

  it('maps a network failure to an error result', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    const result = await getAttachments('conv-1', failing)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })
})

// ── Auth (x5bz.2) ─────────────────────────────────────────────────────────────

describe('signup', () => {
  it('returns ok with the user on 200', async () => {
    const result = await signup(
      'ada@example.com', 'password123', 'tok-abc',
      fakeFetch(200, { email: 'ada@example.com', role: 'dm' }),
    )
    expect(result).toEqual({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  })

  it('sends the invite token and credentials:include', async () => {
    let captured: RequestInit | undefined
    const spy: typeof fetch = (async (_url, init) => {
      captured = init
      return new Response(JSON.stringify({ email: 'a@example.com', role: 'player' }), { status: 200 })
    }) as typeof fetch
    await signup('a@example.com', 'password123', 'tok-xyz', spy)
    expect(captured?.credentials).toBe('include')
    expect(JSON.parse(String(captured?.body))).toEqual({
      email: 'a@example.com', password: 'password123', invite: 'tok-xyz',
    })
  })

  it('surfaces the service detail message on a 400 (bad invite)', async () => {
    const result = await signup(
      'a@example.com', 'password123', 'used-token',
      fakeFetch(400, { detail: 'This invite link has already been used.' }),
    )
    expect(result).toEqual({
      kind: 'error', status: 400, message: 'This invite link has already been used.',
    })
  })

  it('normalizes a FastAPI 422 validation array into a string message', async () => {
    // FastAPI answers 422 with `detail: [{loc, msg, type}, ...]`. Passing that
    // array to React throws ("objects are not valid as a React child"), so a
    // 7-char password used to crash the screen instead of showing an error.
    const result = await signup(
      'a@example.com', 'short', 'tok',
      fakeFetch(422, {
        detail: [
          { type: 'string_too_short', loc: ['body', 'password'],
            msg: 'String should have at least 8 characters' },
        ],
      }),
    )
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(typeof result.message).toBe('string')
      expect(result.message).toMatch(/at least 8 characters/i)
    }
  })

  it('falls back to a generic message when detail is an unexpected shape', async () => {
    const result = await signup('a@example.com', 'password123', 'tok', fakeFetch(400, { detail: {} }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toBe('Request failed (400).')
  })

  it('surfaces a 409 on duplicate email', async () => {
    const result = await signup(
      'dup@example.com', 'password123', 'tok', fakeFetch(409, { detail: 'taken' }),
    )
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.status).toBe(409)
  })

  it('maps a network failure to an error result', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    const result = await signup('a@example.com', 'password123', 'tok', failing)
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/reach|network/i)
  })
})

describe('login', () => {
  it('returns ok with the user on 200', async () => {
    const result = await login(
      'ada@example.com', 'password123',
      fakeFetch(200, { email: 'ada@example.com', role: 'player' }),
    )
    expect(result).toEqual({ kind: 'ok', user: { email: 'ada@example.com', role: 'player' } })
  })

  it('returns a generic 401 error on bad credentials', async () => {
    const result = await login(
      'ada@example.com', 'wrong', fakeFetch(401, { detail: 'invalid email or password' }),
    )
    expect(result).toEqual({ kind: 'error', status: 401, message: 'invalid email or password' })
  })
})

describe('logout', () => {
  it('posts to /auth/logout with credentials:include', async () => {
    let called = false
    let captured: RequestInit | undefined
    const spy: typeof fetch = (async (_url, init) => {
      called = true
      captured = init
      return new Response(null, { status: 200 })
    }) as typeof fetch
    await logout(spy)
    expect(called).toBe(true)
    expect(captured?.method).toBe('POST')
    expect(captured?.credentials).toBe('include')
  })

  it('reports failure (false) instead of throwing when the request fails', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    // The caller must be able to tell — only a successful server response
    // clears the httpOnly cookie.
    await expect(logout(failing)).resolves.toBe(false)
  })

  it('reports failure on a non-2xx response', async () => {
    await expect(logout(fakeFetch(500))).resolves.toBe(false)
  })
})

describe('getMe', () => {
  it('returns ok with the user on 200', async () => {
    const result = await getMe(fakeFetch(200, { email: 'ada@example.com', role: 'dm' }))
    expect(result).toEqual({ kind: 'ok', user: { email: 'ada@example.com', role: 'dm' } })
  })

  it('returns an error (not a throw) on 401 — no session', async () => {
    const result = await getMe(fakeFetch(401))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.status).toBe(401)
  })

  it('maps a network failure to an error result', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    const result = await getMe(failing)
    expect(result.kind).toBe('error')
  })

  // The caller decides logout-vs-outage from `status` alone, so getMe must
  // report it faithfully — and must not describe an outage as "not signed in",
  // which is how the two got conflated in the first place (PR #43 review).

  it.each([500, 502, 503, 403, 404])('reports the real status for %i', async (status) => {
    const result = await getMe(fakeFetch(status))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.status).toBe(status)
      expect(result.message).not.toMatch(/not signed in/i)
    }
  })

  it('leaves status undefined for a network failure (nothing was answered)', async () => {
    const failing: typeof fetch = (async () => {
      throw new TypeError('fetch failed')
    }) as typeof fetch
    const result = await getMe(failing)
    if (result.kind === 'error') expect(result.status).toBeUndefined()
  })

  it('only 401 says "not signed in"', async () => {
    const unauthorized = await getMe(fakeFetch(401))
    if (unauthorized.kind === 'error') expect(unauthorized.message).toMatch(/not signed in/i)
  })
})
