/**
 * api.googleAvailable / api.getGoogleLink (lvs7 pr-b).
 *
 * The button's whole existence hangs on `googleAvailable`: a 404 (the feature is
 * off), any other failure, a network error and any body that is not exactly
 * `{"available": true}` all mean "no button".
 */

import { describe, expect, it, vi } from 'vitest'
import { getGoogleLink, googleAvailable } from './api'

function reply(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })) as unknown as typeof fetch
}

function failing(): typeof fetch {
  return vi.fn(async () => { throw new TypeError('down') }) as unknown as typeof fetch
}

describe('googleAvailable', () => {
  it('is true for 200 {"available": true}, asked of the right path', async () => {
    const fetchImpl = reply({ available: true })
    expect(await googleAvailable(fetchImpl)).toBe(true)
    expect(vi.mocked(fetchImpl).mock.calls[0][0]).toBe('/auth/google/available')
  })

  it('is false for the 404 an unconfigured service answers', async () => {
    expect(await googleAvailable(reply({ detail: 'Not Found' }, 404))).toBe(false)
  })

  it('is false for a 5xx, a network error, and a body that is not exactly true', async () => {
    expect(await googleAvailable(reply({ available: true }, 503))).toBe(false)
    expect(await googleAvailable(failing())).toBe(false)
    for (const body of [{ available: false }, { available: 'true' }, { available: 1 }, {}, [], null, 'yes']) {
      expect(await googleAvailable(reply(body)), JSON.stringify(body)).toBe(false)
    }
    const html = vi.fn(async () => new Response('<html>', { status: 200 })) as unknown as typeof fetch
    expect(await googleAvailable(html)).toBe(false)
  })
})

describe('getGoogleLink', () => {
  it('maps the server shape, sending the session cookie', async () => {
    const fetchImpl = reply({ linked: true, email: 'ada@gmail.example', has_password: false })
    expect(await getGoogleLink(fetchImpl)).toEqual({
      linked: true, email: 'ada@gmail.example', hasPassword: false,
    })
    const [path, init] = vi.mocked(fetchImpl).mock.calls[0]
    expect(path).toBe('/auth/google/link')
    expect((init as RequestInit).credentials).toBe('include')
  })

  it('an unlinked account has a null email', async () => {
    expect(await getGoogleLink(reply({ linked: false, email: null, has_password: true }))).toEqual({
      linked: false, email: null, hasPassword: true,
    })
  })

  it('is null on 401, 404, 503, a network error and a malformed body', async () => {
    for (const status of [401, 404, 503]) {
      expect(await getGoogleLink(reply({ detail: 'x' }, status)), String(status)).toBeNull()
    }
    expect(await getGoogleLink(failing())).toBeNull()
    const malformed = [
      {}, { linked: 'yes', email: null, has_password: true },
      { linked: true, email: 5, has_password: true }, { linked: true, email: null }, [],
    ]
    for (const body of malformed) {
      expect(await getGoogleLink(reply(body)), JSON.stringify(body)).toBeNull()
    }
  })
})
