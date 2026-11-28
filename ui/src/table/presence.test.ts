import { afterEach, expect, it, vi } from 'vitest'
import { tablePresence } from './presence'

afterEach(() => { vi.useRealTimers() })
const context = 'c'.repeat(64)
const handle = 'x'.repeat(32)
const registration = 'r'.repeat(32)

it('serializes rapid changes, renews the actual state, and closes only its registration', async () => {
  vi.useFakeTimers()
  const bodies: Record<string, unknown>[] = []
  let release: () => void = () => {}
  const store = tablePresence('cmp_Table', async (_path, init) => {
    const body = JSON.parse(String(init?.body))
    bodies.push(body)
    if (body.action === 'open') return Response.json({ schema_version: 1, registration })
    if (body.state === 'pending') await new Promise<void>((done) => { release = done })
    return new Response(null, { status: 204 })
  }, () => { throw Error('unexpected refresh') })
  store.authorize(context)
  await vi.advanceTimersByTimeAsync(2)
  expect(bodies.map((b) => b.action)).toEqual(['open', 'report'])
  expect(bodies[1]).toMatchObject({ revision: 1, state: 'pending' })
  store.update('listening', [`/table/assets/${handle}`])
  store.update('muted', [])
  release()
  await vi.advanceTimersByTimeAsync(2)
  expect(bodies[2]).toMatchObject({ revision: 2, state: 'muted', playing_handles: [] })
  store.update('listening', [`/table/assets/${handle}`, '/campaigns/cmp_private/assets/ast_ignored'])
  await vi.advanceTimersByTimeAsync(15_001)
  expect(bodies.slice(3).map((b) => [b.revision, b.state, b.playing_handles])).toEqual([
    [3, 'listening', [handle]], [4, 'listening', [handle]],
  ])
  store.reset()
  await vi.advanceTimersByTimeAsync(45_000)
  expect(bodies.at(-1)).toEqual({ schema_version: 1, action: 'close', registration })
  expect(bodies).toHaveLength(6)
})

it('forgets stale registrations and bounds repeated registration refusals', async () => {
  vi.useFakeTimers()
  const previous: string[] = []
  const store = tablePresence('cmp_Table', async (_path, init) => {
    previous.push(JSON.parse(String(init?.body)).action)
    return new Response(null, { status: 404 })
  }, () => store.authorize(context))
  store.authorize(context)
  await vi.advanceTimersByTimeAsync(12_000)
  expect(previous).toEqual(['open'])
  await vi.advanceTimersByTimeAsync(4000)
  expect(previous).toEqual(['open', 'open'])
  store.reset()
})

it.each([200, 403])('isolates a late open response (%i) after a rapid close and reopen', async (status) => {
  vi.useFakeTimers()
  const bodies: Record<string, unknown>[] = []
  const refresh = vi.fn()
  const oldToken = 'o'.repeat(32)
  let finish!: (response: Response) => void
  const store = tablePresence('cmp_Table', async (_path, init) => {
    const body = JSON.parse(String(init?.body))
    bodies.push(body)
    if (body.action === 'open') {
      if (bodies.length === 1) return new Promise(resolve => { finish = resolve })
      return Response.json({ schema_version: 1, registration })
    }
    return new Response(null, { status: 204 })
  }, refresh)
  store.authorize(context)
  await vi.advanceTimersByTimeAsync(2)
  store.reset()
  store.authorize(context)
  store.update('muted', [])
  await vi.advanceTimersByTimeAsync(2)
  expect(bodies[0].page_id).not.toEqual(bodies[1].page_id)
  expect(bodies[2]).toMatchObject({ action: 'report', registration, revision: 1, state: 'muted' })

  finish(Response.json({ schema_version: 1, registration: oldToken }, { status }))
  await vi.advanceTimersByTimeAsync(2)
  expect(bodies.filter(b => b.action === 'close').map(b => b.registration)).toEqual(status === 200 ? [oldToken] : [])
  expect(refresh).not.toHaveBeenCalled()
  store.update('idle', [])
  await vi.advanceTimersByTimeAsync(2)
  expect(bodies.at(-1)).toMatchObject({ action: 'report', registration, revision: 2, state: 'idle' })
  expect(bodies.some(b => b.registration === oldToken && b.action === 'report')).toBe(false)
  store.reset()
})
