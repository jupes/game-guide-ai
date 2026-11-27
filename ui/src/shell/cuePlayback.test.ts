import { afterEach, describe, expect, it, vi } from 'vitest'
import { CuePlayback } from './cuePlayback'
import { CueStopCourier } from './cueStopCourier'
import { SESSION_REFRESH_EVENT } from './tableSession'

const session = 'tss_Test'
function state(epoch: number, seq = 0, title?: string) {
  const playing = title ? { cue_id: 'cue_Rain', title, started_at: '2026-11-27T19:00:00Z', start_offset_ms: 0, duration_ms: 20000, loop: true } : null
  return Response.json(['ambience', 'one_shot'].map((slot) => ({
    schema_version: 1, event: 'audio', session_id: session, gen: 1, audio_epoch: epoch,
    slot, seq: slot === 'ambience' ? seq : 0, playing: slot === 'ambience' ? playing : null,
  })))
}

function fixture(courier = new CueStopCourier(), account = 'gm') {
  const requests: { path: string; body?: string; finish: (response: Response) => void }[] = []
  const fetchImpl: typeof fetch = (input, init) => new Promise((finish) => {
    requests.push({ path: String(input), body: init?.body as string | undefined, finish })
  })
  const store = new CuePlayback('cmp_A', session, fetchImpl, account, courier)
  const dispose = store.start()
  return { store, requests, dispose }
}

afterEach(() => vi.useRealTimers())

describe('GM table cue commands', () => {
  it('finishes the original Stop after navigation and blocks widening when the GM returns', async () => {
    vi.useFakeTimers()
    const courier = new CueStopCourier()
    const first = fixture(courier)
    first.requests[0].finish(state(1, 1, 'Rain'))
    await vi.advanceTimersByTimeAsync(1)
    const stopped = first.store.stop(null)
    first.requests[1].finish(new Response(null, { status: 503 }))
    await vi.advanceTimersByTimeAsync(1)
    first.dispose()
    const reopened = fixture(courier)
    try {
      reopened.requests[0].finish(state(1, 1, 'Rain'))
      await vi.advanceTimersByTimeAsync(1)
      expect(reopened.store.snapshot().pendingStops).toBe(1)
      await reopened.store.play('cue_Bell', false)
      expect(reopened.requests).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(500)
      expect(first.requests[2].body).toBe(first.requests[1].body)
      first.requests[2].finish(state(2, 2))
      await stopped
      expect(reopened.store.snapshot().pendingStops).toBe(0)
      expect(reopened.store.snapshot().connected).toBe(false)
      reopened.requests[1].finish(state(2, 2))
      await vi.advanceTimersByTimeAsync(1)
      expect(reopened.store.snapshot().frames[0]).toMatchObject({ audio_epoch: 2, playing: null })
      expect(first.store.snapshot().frames).toEqual([])
    } finally { reopened.dispose() }
  })

  it('cancels detached retries when the signed-in account changes', async () => {
    vi.useFakeTimers()
    const courier = new CueStopCourier()
    const first = fixture(courier)
    const stopping = first.store.stop(null)
    first.requests[1].finish(new Response(null, { status: 503 }))
    await vi.advanceTimersByTimeAsync(1)
    first.dispose()
    courier.retain('another-gm')
    await stopping
    await vi.advanceTimersByTimeAsync(5000)
    expect(first.requests).toHaveLength(2)
  })

  it('refreshes on return to a hidden tab and asks for the current session after a definitive refusal', async () => {
    vi.useFakeTimers()
    const { store, requests, dispose } = fixture()
    const refresh = vi.fn()
    window.addEventListener(SESSION_REFRESH_EVENT, refresh)
    const hidden = vi.spyOn(document, 'hidden', 'get')
    try {
      requests[0].finish(state(1))
      await vi.advanceTimersByTimeAsync(1)
      hidden.mockReturnValue(true)
      document.dispatchEvent(new Event('visibilitychange'))
      expect(store.snapshot().connected).toBe(false)
      await vi.advanceTimersByTimeAsync(5000)
      expect(requests).toHaveLength(1)
      hidden.mockReturnValue(false)
      document.dispatchEvent(new Event('visibilitychange'))
      requests[1].finish(new Response(null, { status: 404 }))
      await vi.advanceTimersByTimeAsync(1)
      expect(refresh).toHaveBeenCalledTimes(1)
      expect(store.snapshot().frames).toEqual([])
    } finally {
      dispose()
      hidden.mockRestore()
      window.removeEventListener(SESSION_REFRESH_EVENT, refresh)
    }
  })
  it('keeps a Stop above a late Play response and clears state when the scope closes', async () => {
    const { store, requests, dispose } = fixture()
    try {
      requests[0].finish(state(1))
      await vi.waitFor(() => expect(store.snapshot().connected).toBe(true))
      const playing = store.play('cue_Rain', true)
      const stopping = store.stop(null)
      expect(requests.map((r) => r.path.split('/').at(-1))).toEqual(['audio', 'play', 'stop'])
      expect(JSON.parse(requests[2].body!)).toMatchObject({ cue_id: null })
      requests[2].finish(state(3, 2))
      await stopping
      requests[1].finish(state(2, 1, 'Rain'))
      await playing
      expect(store.snapshot().frames[0]).toMatchObject({ audio_epoch: 3, playing: null })
    } finally { dispose() }
    expect(store.snapshot().frames).toEqual([])
  })

  it('retries an uncertain Stop with the same id and bars Play until it is acknowledged', async () => {
    vi.useFakeTimers()
    const { store, requests, dispose } = fixture()
    try {
      requests[0].finish(state(2, 1, 'Rain'))
      await vi.advanceTimersByTimeAsync(1)
      const stopping = store.stop('cue_Rain')
      requests[1].finish(new Response(null, { status: 503 }))
      await vi.advanceTimersByTimeAsync(501)
      expect(requests[2].body).toBe(requests[1].body)
      await store.play('cue_Bell', false)
      expect(requests).toHaveLength(3)
      requests[2].finish(state(3, 2))
      await stopping
      expect(store.snapshot()).toMatchObject({ pendingStops: 0, stopError: null })
    } finally { dispose() }
  })

  it('reuses an uncertain Play on manual retry and refreshes a stale epoch without replaying', async () => {
    const { store, requests, dispose } = fixture()
    try {
      requests[0].finish(state(1))
      await vi.waitFor(() => expect(store.snapshot().connected).toBe(true))
      const first = store.play('cue_Rain', true)
      requests[1].finish(new Response(null, { status: 503 }))
      await first
      const retry = store.play('cue_Rain', true, true)
      expect(requests[2].body).toBe(requests[1].body)
      requests[2].finish(new Response(null, { status: 409 }))
      await vi.waitFor(() => expect(requests).toHaveLength(4))
      expect(requests[3].body).toBeUndefined()
      requests[3].finish(state(4))
      await retry
      expect(store.snapshot()).toMatchObject({ failedPlay: null, frames: [{ audio_epoch: 4 }, { audio_epoch: 4 }] })
    } finally { dispose() }
  })
})
