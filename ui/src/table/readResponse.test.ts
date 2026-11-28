import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { tableReadRetryAt } from './readResponse'
import { streamTable } from './stream'

const now = Date.UTC(2026, 10, 28, 9)

describe('a table read refusal', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(now)
  })
  afterEach(() => vi.useRealTimers())

  it.each([
    ['25', 25_000], ['0', 1000], [new Date(now + 45_000).toUTCString(), 45_000],
    ['Saturday, 28-Nov-26 09:00:45 GMT', 45_000], ['Sat Nov 28 09:00:45 2026', 45_000],
    [new Date(now - 1000).toUTCString(), 1000], ['600', 600_000],
    ['-1', 5000], ['1.5', 5000], ['invalid', 5000], ['January 2099', 5000],
    ['garbage, 2099', 5000], ['Thu, 31 Nov 2099 12:00:00 GMT', 5000], [null, 5000],
  ] as const)('respects the Retry-After header %s', (header, expectedDelay) => {
    const response = new Response(null, { status: 429, headers: header === null ? {} : { 'Retry-After': header } })
    expect(tableReadRetryAt(response)).toBe(now + expectedDelay)
  })

  it('keeps a very long delay representable as a future deadline', () => {
    const response = new Response(null, { status: 429, headers: { 'Retry-After': '9'.repeat(400) } })
    expect(tableReadRetryAt(response)).toBe(Number.MAX_SAFE_INTEGER)
  })

  it.each([
    ['registered table denial', 'application/json; charset=utf-8',
      JSON.stringify({ detail: { code: 'inactive', message: 'This table is unavailable.', retryable: false } }), 'inactive', true],
    ['unmarked table denial', 'application/json',
      JSON.stringify({ detail: { code: 'inactive', message: 'This table is unavailable.', retryable: false } }), 'inactive', false],
    ['unregistered API route', 'application/json', JSON.stringify({ detail: 'Not Found' }), 'retry', false],
    ['another typed error', 'application/json',
      JSON.stringify({ detail: { code: 'not_found', message: 'Not found', retryable: false } }), 'retry', false],
    ['invalid JSON', 'application/json', '{', 'retry', false],
    ['proxy response', 'text/html', '<title>Not found</title>', 'retry', false],
  ] as const)('distinguishes %s from a missing stream transport', async (_name, contentType, body, kind, checksAccess) => {
    const headers = new Headers({ 'Content-Type': contentType })
    if (checksAccess) {
      headers.set('Cache-Control', 'private, no-store')
      headers.set('Cross-Origin-Resource-Policy', 'same-origin')
    }
    const response = new Response(body, { status: 404, headers })
    const updates = { checkingAccess: vi.fn(), opened: vi.fn(), bytes: vi.fn(), picture: vi.fn() }
    const result = await streamTable('cmp_table', new AbortController().signal, async () => response, updates)
    expect(result).toEqual({ kind })
    expect(updates.checkingAccess).toHaveBeenCalledTimes(checksAccess ? 1 : 0)
    expect(updates.opened).not.toHaveBeenCalled()
    expect(updates.picture).not.toHaveBeenCalled()
  })
})
