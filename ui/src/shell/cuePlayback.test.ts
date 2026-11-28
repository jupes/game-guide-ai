import { afterEach, describe, expect, it, vi } from 'vitest'
import { CuePlayback } from './cuePlayback'
import { CUE_STATE_SILENCE_MS } from './cueStream'
import { CueStopCourier } from './cueStopCourier'
import { SESSION_REFRESH_EVENT } from './tableSession'

const session = 'tss_Test'
function audioFrames(epoch: number, seq = 0, title?: string) {
  const playing = title ? { cue_id: 'cue_Rain', title, started_at: '2026-11-27T19:00:00Z', start_offset_ms: 0, duration_ms: 20000, loop: true } : null
  return ['ambience', 'one_shot'].map((slot) => ({
    schema_version: 1, event: 'audio', session_id: session, gen: 1, audio_epoch: epoch,
    slot, seq: slot === 'ambience' ? seq : 0, playing: slot === 'ambience' ? playing : null,
  }))
}

const state = (epoch: number, seq = 0, title?: string) => Response.json(audioFrames(epoch, seq, title))
function sessionFrame(epoch: number) {
  return { schema_version: 1, event: 'session', session: {
    schema_version: 1, session_id: session, campaign_id: 'cmp_A', state: 'live', gen: 1,
    reveal_epoch: 0, audio_epoch: epoch, audio: true, screens: [],
    started_at: '2026-11-27T19:00:00Z', ends_at: '2026-11-27T23:00:00Z', ended_at: null,
  } }
}
const reveal = { schema_version: 1, event: 'snapshot', state: {
  session_id: session, gen: 1, reveal_epoch: 0, slots: [{ slot: { kind: 'table' }, seq: 0, live: null }],
} }
const ready = { schema_version: 1, event: 'ready' }
const presence = { schema_version: 1, event: 'presence', session_id: session, gen: 1,
  participants: [], guests: { connected: 1, listening: 0, muted: 0, pending: 1 } }

function streamed() {
  let writer!: ReadableStreamDefaultController<Uint8Array>
  const paths: string[] = []
  const stream = new ReadableStream<Uint8Array>({ start(controller) { writer = controller } })
  const fetchImpl: typeof fetch = async (input) => {
    const path = String(input)
    paths.push(path)
    if (path.endsWith('/presence')) return Response.json(presence)
    if (!path.endsWith('/stream')) return state(4, 3)
    return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
  }
  const store = new CuePlayback('cmp_A', session, fetchImpl)
  const dispose = store.start()
  const send = (...frames: { event: string }[]) => {
    for (const frame of frames) writer.enqueue(new TextEncoder().encode(`event: ${frame.event}\ndata: ${JSON.stringify(frame)}\n\n`))
  }
  return { store, dispose, paths, send }
}
async function fixture(courier = new CueStopCourier(), account = 'gm') {
  const requests: { path: string; body?: string; finish: (response: Response) => void }[] = []
  const stream = vi.fn(async () => new Response(null, { status: 503 }))
  const fetchImpl: typeof fetch = (input, init) => {
    if (String(input).endsWith('/stream')) return stream()
    if (String(input).endsWith('/presence')) return Promise.resolve(Response.json(presence))
    return new Promise((finish) => requests.push({ path: String(input), body: init?.body as string | undefined, finish }))
  }
  const store = new CuePlayback('cmp_A', session, fetchImpl, account, courier)
  const dispose = store.start()
  await vi.waitFor(() => expect(requests).toHaveLength(1))
  return { store, requests, stream, dispose }
}

afterEach(() => vi.useRealTimers())

describe('GM table cue commands', () => {
  it('keeps fresh polling state and listener feedback through repeated unsupported stream probes', async () => {
    vi.useFakeTimers()
    const { store, requests, stream, dispose } = await fixture()
    try {
      stream.mockImplementation(async () => new Response(null, { status: 404 }))
      requests[0].finish(state(4))
      await vi.advanceTimersByTimeAsync(0)
      const reports: boolean[] = []
      store.subscribe(() => reports.push(store.snapshot().connected && store.snapshot().presence !== null))
      await vi.advanceTimersByTimeAsync(1300)
      expect(stream).toHaveBeenCalledTimes(2)
      expect(store.snapshot()).toMatchObject({ connected: true, presence })
      requests[1].finish(state(4))
      await vi.advanceTimersByTimeAsync(2600)
      expect(stream).toHaveBeenCalledTimes(3)
      expect(reports).toContain(true)
      expect(reports).not.toContain(false)
      requests[2].finish(new Response(null, { status: 503 }))
      await vi.advanceTimersByTimeAsync(0)
      expect(store.snapshot()).toMatchObject({ connected: false, presence: null })
      expect(reports.at(-1)).toBe(false)
    } finally { dispose() }
  })

  it.each(['hanging stream probe', 'hanging poll', 'regressing poll'] as const)(
    'expires the last verified poll while a %s cannot refresh it', async (fault) => {
      vi.useFakeTimers()
      const { store, requests, stream, dispose } = await fixture()
      try {
        requests[0].finish(state(4, 3))
        await vi.advanceTimersByTimeAsync(0)
        if (fault === 'hanging stream probe') stream.mockImplementation(() => new Promise(() => {}))
        await vi.advanceTimersByTimeAsync(1300)
        if (fault === 'regressing poll') requests[1].finish(state(3, 2))
        await vi.advanceTimersByTimeAsync(CUE_STATE_SILENCE_MS - 1301)
        expect(store.snapshot()).toMatchObject({ connected: true, presence })
        await vi.advanceTimersByTimeAsync(1)
        expect(store.snapshot()).toMatchObject({ connected: false, presence: null })
        expect(store.snapshot().frames[0].audio_epoch).toBe(4)
        const pending = requests.length
        await store.play('cue_Rain', true)
        expect(requests).toHaveLength(pending)
      } finally { dispose() }
    },
  )

  it('withdraws a fresh polling snapshot immediately on authoritative stream refusal', async () => {
    vi.useFakeTimers()
    const { store, requests, stream, dispose } = await fixture()
    try {
      requests[0].finish(state(4))
      await vi.advanceTimersByTimeAsync(0)
      expect(store.snapshot()).toMatchObject({ connected: true, presence })
      stream.mockImplementation(async () => new Response(null, { status: 403 }))
      await vi.advanceTimersByTimeAsync(1300)
      expect(store.snapshot()).toMatchObject({ connected: false, frames: [], presence: null })
      expect(requests).toHaveLength(1)
    } finally { dispose() }
  })

  it('falls back immediately when an opening stream snapshot is older than the accepted poll', async () => {
    vi.useFakeTimers()
    const { store, requests, stream, dispose } = await fixture()
    try {
      requests[0].finish(state(4, 3))
      await vi.advanceTimersByTimeAsync(0)
      const oldPresence = { ...presence, guests: { connected: 2, listening: 0, muted: 0, pending: 2 } }
      const opening = [sessionFrame(3), reveal, ...audioFrames(3, 2), oldPresence, ready]
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        const text = opening.map((frame) => `event: ${frame.event}\ndata: ${JSON.stringify(frame)}\n\n`).join('')
        controller.enqueue(new TextEncoder().encode(text))
      } })
      stream.mockResolvedValueOnce(new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }))
      const seen: number[] = []
      store.subscribe(() => seen.push(store.snapshot().presence?.guests.connected ?? 0))
      await vi.advanceTimersByTimeAsync(1300)
      expect(requests).toHaveLength(2)
      expect(store.snapshot()).toMatchObject({ connected: true, presence })
      expect(store.snapshot().frames[0].audio_epoch).toBe(4)
      requests[1].finish(state(5, 4))
      await vi.advanceTimersByTimeAsync(0)
      expect(store.snapshot().frames[0].audio_epoch).toBe(5)
      expect(seen).not.toContain(2)
    } finally { dispose() }
  })

  it('updates listener feedback without restarting either live cue', async () => {
    vi.useFakeTimers()
    const { store, dispose, paths, send } = streamed()
    try {
      send(sessionFrame(1), reveal, ...audioFrames(1), presence, ready)
      await vi.advanceTimersByTimeAsync(1)
      const prior = store.snapshot().frames
      expect(store.snapshot().presence?.guests.pending).toBe(1)
      const muted = { ...presence, guests: { connected: 1, listening: 0, muted: 1, pending: 0 } }
      send(muted)
      await vi.advanceTimersByTimeAsync(1)
      expect(store.snapshot().presence?.guests.muted).toBe(1)
      expect(store.snapshot().frames).toBe(prior)
      expect(paths).toEqual(['/campaigns/cmp_A/stream'])
    } finally { dispose() }
  })
  it('delivers Off immediately during Enable and retries Off with its original command id', async () => {
    vi.useFakeTimers()
    const { store, requests, dispose } = await fixture()
    try {
      requests[0].finish(state(1))
      await vi.advanceTimersByTimeAsync(2)
      const enabling = store.setEnabled(true)
      const disabling = store.setEnabled(false)
      expect(store.snapshot()).toMatchObject({ enabling: false, disabling: true, pendingStops: 1 })
      expect(JSON.parse(requests[1].body!)).toMatchObject({ enabled: true, audio_epoch: 1 })
      expect(JSON.parse(requests[2].body!)).toMatchObject({ enabled: false })
      requests[2].finish(new Response(null, { status: 503 }))
      requests[1].finish(Response.json({ schema_version: 1, enabled: true, audio_epoch: 2 }))
      await enabling
      expect(store.snapshot().audioEnabled).not.toBe(true)
      await vi.advanceTimersByTimeAsync(501)
      expect(requests[3].body).toBe(requests[2].body)
      requests[3].finish(Response.json({ schema_version: 1, enabled: false, audio_epoch: 3 }))
      expect(await disabling).toBe(true)
      expect(store.snapshot()).toMatchObject({ enabling: false, disabling: false, pendingStops: 0, audioEnabled: false })
    } finally { dispose() }
  })
  it('applies only complete ready snapshots and ordered live pairs, then recovers a sequence gap', async () => {
    vi.useFakeTimers()
    const { store, dispose, paths, send } = streamed()
    try {
      send(sessionFrame(1), reveal, ...audioFrames(1))
      await vi.advanceTimersByTimeAsync(1)
      expect(store.snapshot()).toMatchObject({ connected: false, frames: [] })
      send(ready)
      await vi.advanceTimersByTimeAsync(1)
      expect(store.snapshot().frames[0].audio_epoch).toBe(1)
      const pair = audioFrames(2, 1, 'Rain')
      send(sessionFrame(2), pair[0])
      await vi.advanceTimersByTimeAsync(1)
      expect(store.snapshot().frames[0].playing).toBeNull()
      send(pair[1])
      await vi.advanceTimersByTimeAsync(1)
      expect(store.snapshot().frames[0].playing?.title).toBe('Rain')
      expect(paths).toEqual(['/campaigns/cmp_A/stream'])
      send(sessionFrame(3), ...audioFrames(3, 3, 'Skipped state'))
      await vi.advanceTimersByTimeAsync(1)
      expect(paths.slice(-2)).toEqual(['/campaigns/cmp_A/sessions/tss_Test/audio', '/campaigns/cmp_A/sessions/tss_Test/audio/presence'])
      expect(store.snapshot().presence).toEqual(presence)
      expect(store.snapshot().frames[0]).toMatchObject({ audio_epoch: 4, playing: null })
    } finally { dispose() }
  })

  it('recovers a silent stream through the snapshot and retries the stream', async () => {
    vi.useFakeTimers()
    const { store, dispose, paths, send } = streamed()
    try {
      send(sessionFrame(1), reveal, ...audioFrames(1), ready)
      await vi.advanceTimersByTimeAsync(10_001)
      expect(store.snapshot().frames[0].audio_epoch).toBe(4)
      expect(paths.filter((p) => p.endsWith('/stream'))).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(1250)
      expect(paths.filter((p) => p.endsWith('/stream')).length).toBeGreaterThan(1)
    } finally { dispose() }
  })

  it.each(['generation', 'epoch', 'slot'] as const)('refuses an opening snapshot with a mixed %s', async (problem) => {
    vi.useFakeTimers()
    const { store, dispose, paths, send } = streamed()
    try {
      const frames = audioFrames(1, 1, 'Wrong scope')
      if (problem === 'generation') frames[1].gen = 2
      if (problem === 'epoch') frames[1].audio_epoch = 2
      if (problem === 'slot') frames[1].slot = 'ambience'
      const seen: string[] = []
      store.subscribe(() => { for (const frame of store.snapshot().frames) if (frame.playing) seen.push(frame.playing.title) })
      send(sessionFrame(1), reveal, ...frames, ready)
      await vi.advanceTimersByTimeAsync(1)
      expect(seen).not.toContain('Wrong scope')
      expect(paths.slice(-2).map((path) => path.split('/').at(-1))).toEqual(['audio', 'presence'])
      expect(store.snapshot().frames[0].audio_epoch).toBe(4)
    } finally { dispose() }
  })

  it('honours the server retry delay and immediately resends the same Stop on an explicit press', async () => {
    vi.useFakeTimers()
    const { store, requests, dispose } = await fixture()
    try {
      const stopping = store.stop(null)
      requests[1].finish(new Response(null, { status: 429, headers: { 'Retry-After': '12' } }))
      await vi.advanceTimersByTimeAsync(5000)
      expect(requests.filter((r) => r.path.endsWith('/stop'))).toHaveLength(1)
      const again = store.stop(null)
      const stops = requests.filter((r) => r.path.endsWith('/stop'))
      expect(stops).toHaveLength(2)
      expect(stops[1].body).toBe(stops[0].body)
      stops[1].finish(new Response(null, { status: 503, headers: { 'Retry-After': '12' } }))
      await vi.advanceTimersByTimeAsync(11_999)
      expect(requests.filter((r) => r.path.endsWith('/stop'))).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(2)
      const last = requests.filter((r) => r.path.endsWith('/stop')).at(-1)!
      expect(last).not.toBe(stops[1])
      expect(last.body).toBe(stops[0].body)
      last.finish(state(4))
      await Promise.all([stopping, again])
    } finally { dispose() }
  })
  it('finishes the original Stop after navigation and blocks widening when the GM returns', async () => {
    vi.useFakeTimers()
    const courier = new CueStopCourier()
    const first = await fixture(courier)
    first.requests[0].finish(state(1, 1, 'Rain'))
    await vi.advanceTimersByTimeAsync(1)
    const stopped = first.store.stop(null)
    first.requests[1].finish(new Response(null, { status: 503 }))
    await vi.advanceTimersByTimeAsync(1)
    first.dispose()
    const reopened = await fixture(courier)
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
      await vi.waitFor(() => expect(reopened.requests).toHaveLength(2))
      reopened.requests[1].finish(state(2, 2))
      await vi.advanceTimersByTimeAsync(1)
      expect(reopened.store.snapshot().frames[0]).toMatchObject({ audio_epoch: 2, playing: null })
      expect(first.store.snapshot().frames).toEqual([])
    } finally { reopened.dispose() }
  })

  it('cancels detached retries when the signed-in account changes', async () => {
    vi.useFakeTimers()
    const courier = new CueStopCourier()
    const first = await fixture(courier)
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
    const { store, requests, dispose } = await fixture()
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
      await vi.waitFor(() => expect(requests).toHaveLength(2))
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
    const { store, requests, dispose } = await fixture()
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
    const { store, requests, dispose } = await fixture()
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
    const { store, requests, dispose } = await fixture()
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
