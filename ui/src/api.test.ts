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

/** A response with headers — the throttle contract lives in them, not the body. */
function fakeFetchWithHeaders(
  status: number, headers: Record<string, string>, body?: unknown,
): typeof fetch {
  return (async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json', ...headers },
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

  // ── The chat cost guard (x5bz.3) ──────────────────────────────────────────
  // Three different 429s reach this client and they are not the same event.
  // Two are our limiter (per-tester budget, global daily cap) and say so with
  // X-Chat-Throttled; the third is Cloud Run's own, emitted when no instance is
  // available. Telling a tester to "slow down" when the platform is simply busy
  // would be a lie they can act on wrongly.

  it('maps a per-tester throttle to a slow-down message carrying the wait', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetchWithHeaders(
      429, { 'X-Chat-Throttled': 'user', 'Retry-After': '45' },
      { detail: 'too fast' },
    ))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.outcome).toBe('throttled')
      expect(result.message).toMatch(/45 seconds/i)
      expect(result.message).not.toMatch(/tomorrow|today/i)
    }
  })

  it('rounds a long wait to minutes rather than reciting seconds', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetchWithHeaders(
      429, { 'X-Chat-Throttled': 'user', 'Retry-After': '90' },
    ))
    if (result.kind === 'error') expect(result.message).toMatch(/2 minutes/i)
  })

  it('falls back to a vague wait when Retry-After is missing or nonsense', async () => {
    // The header is not guaranteed — an intermediary can strip it. "shortly" is
    // honest; "in NaN seconds" is what a bare Number() would have produced.
    const result = await postChat('Q', 'sage', null, fakeFetchWithHeaders(
      429, { 'X-Chat-Throttled': 'user' },
    ))
    if (result.kind === 'error') {
      expect(result.message).toMatch(/shortly/i)
      expect(result.message).not.toMatch(/NaN|Infinity|undefined/i)
    }
  })

  it('maps the daily cap to a closed-for-today message, not a slow-down', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetchWithHeaders(
      429, { 'X-Chat-Throttled': 'daily' }, { detail: 'cap reached' },
    ))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.outcome).toBe('throttled')
      expect(result.message).toMatch(/today|tomorrow/i)
    }
  })

  it("does not blame the tester for the platform's own 429", async () => {
    // No marker header: Cloud Run had no instance available. Nothing the tester
    // did caused it and nothing they do fixes it faster.
    const result = await postChat('Q', 'sage', null, fakeFetch(429, { detail: 'no instance' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.message).toMatch(/busy/i)
      expect(result.message).not.toMatch(/your |you're|you have/i)
    }
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

  // ── D4 normalized error/status mapping (b8o.2 Checkpoint 2) ────────────────
  // service/app.py's ERROR_STATUS table: rate_limit/content_filter/
  // invalid_request/authentication/quota/timeout/upstream_unavailable/unknown,
  // plus conversation_strategy_mismatch (409, not part of that table but the
  // same D4 contract). Body shape is {detail: {category, retryable, message}}.

  it('maps 409 to a start-new-conversation recovery outcome', async () => {
    const result = await postChat('Q', 'sage', 'conv-1', fakeFetch(409, { detail: 'mismatch' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.outcome).toBe('conversation_mismatch')
      expect(result.retryable).toBe(false)
      expect(result.message).toMatch(/new conversation/i)
    }
  })

  it('maps a structured 422 (content_filter) using its category, not the generic prompt message', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetch(422, {
      detail: { category: 'content_filter', retryable: false, message: 'refused' },
    }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.retryable).toBe(false)
      expect(result.message).toMatch(/content policy/i)
      expect(result.message).not.toMatch(/enter a question/i)
    }
  })

  it('still maps a plain-string 422 (e.g. empty prompt) to the generic validation message', async () => {
    // Legacy shape — FastAPI's own Pydantic-validation 422 isn't {category, ...}.
    const result = await postChat('', 'sage', null, fakeFetch(422, { detail: 'invalid' }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/prompt/i)
  })

  it('maps a structured 502 (authentication) using its category, retryable=false', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetch(502, {
      detail: { category: 'authentication', retryable: false, message: 'bad creds' },
    }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.retryable).toBe(false)
      expect(result.message).toMatch(/unavailable/i)
    }
  })

  it('maps a structured 502 (upstream_unavailable) as retryable', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetch(502, {
      detail: { category: 'upstream_unavailable', retryable: true, message: 'down' },
    }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.retryable).toBe(true)
  })

  it('a provider rate_limit (structured 429, no X-Chat-Throttled header) is distinct from our own throttle', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetchWithHeaders(429, {}, {
      detail: { category: 'rate_limit', retryable: true, message: 'provider limited' },
    }))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') {
      expect(result.outcome).not.toBe('throttled')
      expect(result.retryable).toBe(true)
    }
  })

  it('an unstructured 502 (no JSON body) still falls back to the generic unavailable message', async () => {
    const result = await postChat('Q', 'sage', null, fakeFetch(502))
    expect(result.kind).toBe('error')
    if (result.kind === 'error') expect(result.message).toMatch(/unavailable|unexpected/i)
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
    // array to React throws ("objects are not valid as a React child"), so it
    // must be normalised to a string before it reaches the screen.
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
  // report it faithfully and must never describe an outage as "not signed in".

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
