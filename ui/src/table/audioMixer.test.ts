import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mountTableAudio } from './audioControls'
import { createAudioMixer, cuePosition, emptyAudioSlots, type MixerCue, type MixerState } from './audioMixer'
import tableHtml from '../../table.html?raw'
import { mountTable } from './table'

const T0 = Date.parse('2026-01-01T12:00:00Z')
function cue(char: string, extra: Partial<MixerCue> = {}): MixerCue {
  return { url: '/table/assets/' + char.repeat(32), started_at: new Date(T0).toISOString(),
    duration_ms: 20_000, start_offset_ms: 0, loop: true, ...extra }
}
function node() {
  return { connect: vi.fn(), disconnect: vi.fn(), gain: {
    value: 1, setValueAtTime: vi.fn(), cancelScheduledValues: vi.fn(), linearRampToValueAtTime: vi.fn(),
  } }
}
class Context {
  static latest: Context
  state = 'suspended'
  currentTime = 0
  destination = {}
  gains: ReturnType<typeof node>[] = []
  elements: HTMLAudioElement[] = []
  close = vi.fn(async () => {})
  resume = vi.fn(async () => { this.state = 'running' })
  onstatechange?: () => void
  constructor() { Context.latest = this }
  createGain() { const result = node(); this.gains.push(result); return result }
  createMediaElementSource(media: HTMLAudioElement) { this.elements.push(media); return node() }
}
const set = (element: object, key: string, value: unknown) => Object.defineProperty(element, key, { configurable: true, value })
const next = () => vi.advanceTimersByTimeAsync(0)
let played: string[]
let fail: 'permission' | 'format' | 'primer' | undefined
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('AudioContext', Context)
  fail = undefined
  played = []
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (this: HTMLMediaElement) {
    played.push(this.src)
    const primer = this.src.includes('audio-unlock')
    if ((primer && fail === 'primer') || (!primer && fail)) {
      return Promise.reject(new DOMException('', fail === 'format' ? 'NotSupportedError' : 'NotAllowedError'))
    }
    set(this, 'paused', false)
    return Promise.resolve()
  })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function (this: HTMLMediaElement) { set(this, 'paused', true) })
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
})
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren() })

function sounding(media: HTMLAudioElement) {
  set(media, 'readyState', 4)
  set(media, 'paused', false)
  media.dispatchEvent(new Event('loadedmetadata'))
  media.dispatchEvent(new Event('playing'))
}
function setup() {
  let now = T0
  let last: MixerState | undefined
  const mixer = createAudioMixer(document, state => { last = state }, () => now)
  return { mixer, state: () => last!, setTime: (ms: number) => { now = ms } }
}

it('cuts buffered audio on a terminal or changed poll context and carries the player’s mute into authorized playback', async () => {
  vi.setSystemTime(T0)
  document.body.innerHTML = new DOMParser().parseFromString(tableHtml, 'text/html').body.innerHTML
  const contexts = ['a', 'b', 'c'].map(c => c.repeat(64))
  const handles = ['x', 'y', 'z'].map(c => c.repeat(32))
  const reports: { action: string; context?: string; registration?: string; state?: string }[] = []
  const snapshot = (stage: number) => {
    const frames = [
      { event: 'session', role: 'guest', audio: true },
      { event: 'snapshot', slots: [{ slot: 'table', seq: 1, content: { content_kind: 'document', type: 'npc',
        fields: [{ key: 'name', value: `Handout ${stage}` }] } }] },
      ...(['ambience', 'one_shot'] as const).map(slot => ({ event: 'audio', slot, seq: 1, playing: {
        asset: { kind: 'audio', handle: handles[stage], media_type: 'audio/mpeg', duration_ms: 20_000, width: null, height: null },
        started_at: new Date(T0 - 2000).toISOString(), start_offset_ms: 0, loop: slot === 'ambience', duration_ms: 20_000,
      } })), { event: 'ready' },
    ]
    return frames.map(f => ({ ...f, schema_version: 1 }))
  }
  let writer!: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({ start(c) { writer = c } })
  function send(frame: { event: string; schema_version: number }): void {
    writer.enqueue(new TextEncoder().encode(`event: ${frame.event}\ndata: ${JSON.stringify(frame)}\n\n`))
  }
  let resume!: (response: Response) => void
  const streams = vi.fn<() => Promise<Response>>()
    .mockResolvedValueOnce(new Response(stream, { headers: { 'Content-Type': 'text/event-stream', 'X-Table-Audio-Context': contexts[0] } }))
    .mockImplementationOnce(() => new Promise(resolve => { resume = resolve }))
    .mockImplementation(async () => new Response(null, { status: 404 }))
  let stage = 1
  let stall = false
  let freshAt = T0
  const stop = mountTable(document, 'cmp_table', async (input, init) => {
    if (String(input).startsWith('/table/stream?')) return streams()
    if (String(input).startsWith('/table/snapshot?')) {
      if (stall) return new Promise(() => {})
      freshAt = Date.now()
      return Response.json({ schema_version: 1, frames: snapshot(stage) },
        { headers: { 'X-Table-Audio-Context': contexts[stage] } })
    }
    const body = JSON.parse(String(init?.body))
    reports.push(body)
    if (body.action === 'open') return Response.json({ schema_version: 1, registration: body.context.slice(0, 32) })
    return new Response(null, { status: 204 })
  }, () => null)

  try {
    for (const frame of snapshot(0)) send(frame)
    await vi.advanceTimersByTimeAsync(2)
    const [accept, mute] = document.querySelectorAll<HTMLButtonElement>('#table-sound button')
    accept.click()
    await next()
    const context = Context.latest
    for (const media of context.elements.filter(el => el.hasAttribute('src'))) sounding(media)
    mute.click()
    await vi.advanceTimersByTimeAsync(2)
    expect(reports.at(-1)).toMatchObject({ action: 'report', state: 'muted', registration: 'a'.repeat(32) })
    const mediaAt = (n: number) => context.elements.filter(media => media.src.endsWith(`/table/assets/${handles[n]}`))
    expect(mediaAt(0)).toHaveLength(2)

    send({ event: 'reconnect', schema_version: 1 })
    await vi.advanceTimersByTimeAsync(2)
    expect(document.querySelector('#table-projections')).toBeEmptyDOMElement()
    expect(context.elements.every(media => media.paused && !media.hasAttribute('src'))).toBe(true)
    expect(reports.filter(r => r.action === 'close')).toEqual([{ schema_version: 1, action: 'close', registration: 'a'.repeat(32) }])
    resume(new Response(null, { status: 404 }))
    await vi.advanceTimersByTimeAsync(3)
    expect(document.querySelector('#table-projections')).toHaveTextContent('Handout 1')
    expect(accept.hidden).toBe(true)
    expect(mute).toHaveAttribute('aria-pressed', 'true')
    expect(Context.latest).toBe(context)
    expect(mediaAt(1)).toHaveLength(2)
    for (const media of mediaAt(1)) sounding(media)
    await vi.advanceTimersByTimeAsync(5000)
    expect(played.filter(url => url.endsWith(handles[1]))).toHaveLength(2)

    stage = 2 // Rotate again while only polling is available.
    await vi.advanceTimersByTimeAsync(5000)
    expect(document.querySelector('#table-projections')).toHaveTextContent('Handout 2')
    expect(mediaAt(1)).toHaveLength(0)
    expect(mediaAt(2)).toHaveLength(2)
    expect(reports.filter(r => r.action === 'close').map(r => r.registration)).toEqual(['a'.repeat(32), 'b'.repeat(32)])
    expect(reports.at(-1)).toMatchObject({ state: 'muted', registration: 'c'.repeat(32) })
    stall = true
    mute.click()
    const ambience = mediaAt(2).find(media => media.loop)!
    sounding(ambience)
    await vi.advanceTimersByTimeAsync(freshAt + 9999 - Date.now())
    expect(ambience.paused).toBe(false)
    expect(document.querySelector('#table-projections')).toHaveTextContent('Handout 2')
    await vi.advanceTimersByTimeAsync(1)
    expect(document.querySelector('#table-projections')).toBeEmptyDOMElement()
    const gain = context.gains[2 + context.elements.indexOf(ambience)].gain
    expect(gain.linearRampToValueAtTime).toHaveBeenLastCalledWith(0, 0.3)
    await vi.advanceTimersByTimeAsync(300)
    expect(ambience.paused).toBe(true)
    expect(ambience.hasAttribute('src')).toBe(false)
  } finally { stop() }
})

describe('the two-slot Web Audio mixer', () => {
  it('calculates from the supplied server clock and applies the late join threshold to one-shots only', () => {
    expect(cuePosition(cue('a', { start_offset_ms: 500 }), T0 + 21_000)).toBe(1.5)
    const shot = cue('b', { loop: false })
    expect(cuePosition(shot, T0 + 19_000, true)).toBe(19)
    expect(cuePosition(shot, T0 + 19_001, true)).toBeNull()
    expect(cuePosition(shot, T0 + 19_100)).toBe(19.1)
    expect(cuePosition(shot, T0 + 20_001)).toBeNull()
  })

  it('primes three silent elements inside the gesture, then seeks and streams only the authorized cue', async () => {
    const { mixer, setTime, state } = setup()
    const a = cue('a')
    mixer.apply({ ...emptyAudioSlots, ambience: a })
    expect(played).toEqual([])
    expect(state().presence).toBe('pending')
    const unlocking = mixer.unlock()
    expect(played).toHaveLength(3)
    expect(played.every(url => url.includes('audio-unlock') && !url.startsWith('data:'))).toBe(true)
    await unlocking
    const [media] = Context.latest.elements
    expect(played[3]).toBe(new URL(a.url, location.href).href)
    expect(state().presence).toBe('idle')
    setTime(T0 + 24_500)
    sounding(media)
    expect(media.loop).toBe(true)
    expect(media.currentTime).toBe(4.5)
    expect(state().playing).toEqual([a.url])
    expect(state().presence).toBe('listening')
    media.currentTime = 6
    mixer.apply({ ...emptyAudioSlots, ambience: { ...a } })
    expect(media.currentTime).toBe(6)
    mixer.apply({ ...emptyAudioSlots, ambience: a }, { resync: true })
    expect(media.currentTime).toBe(4.5)
    media.dispatchEvent(new Event('waiting'))
    expect(state().presence).toBe('idle')
    mixer.dispose()
    expect(Context.latest.close).toHaveBeenCalledOnce()
    expect(media.hasAttribute('src')).toBe(false)
  })

  it('crossfades ambience, ducks a playing shot and stops both halves of a crossfade within 300 ms', async () => {
    const { mixer, state } = setup()
    await mixer.unlock()
    const context = Context.latest
    const [old, incoming, shot] = context.elements
    const [master, duck, oldGain, incomingGain] = context.gains
    mixer.apply({ ambience: cue('a'), one_shot: null })
    sounding(old)
    const b = cue('b'), c = cue('c', { loop: false })
    mixer.apply({ ambience: b, one_shot: c })
    expect(oldGain.gain.linearRampToValueAtTime).toHaveBeenCalledWith(0, 1.5)
    expect(incomingGain.gain.linearRampToValueAtTime).toHaveBeenCalledWith(1, 1.5)
    sounding(incoming)
    sounding(shot)
    expect(duck.gain.linearRampToValueAtTime).toHaveBeenCalledWith(0.35, 0.25)
    expect(state().playing).toEqual([b.url, c.url])
    mixer.setVolume(0.5)
    expect(master.gain.setValueAtTime).toHaveBeenLastCalledWith(0.5, 0)
    mixer.setMuted(true)
    expect(master.gain.setValueAtTime).toHaveBeenLastCalledWith(0, 0)
    expect(state().presence).toBe('muted')
    mixer.setMuted(false)
    expect(state().presence).toBe('listening')
    mixer.apply(emptyAudioSlots)
    expect(duck.gain.linearRampToValueAtTime).toHaveBeenCalledWith(1, 0.6)
    await vi.advanceTimersByTimeAsync(301)
    expect(context.elements.every(el => el.paused && !el.hasAttribute('src'))).toBe(true)
    expect(state().presence).toBe('idle')
    mixer.dispose()
  })

  it('recognizes WebKit resuming after a seek with moving media time and no new playing event', async () => {
    const { mixer, state } = setup()
    await mixer.unlock()
    mixer.apply({ ...emptyAudioSlots, ambience: cue('a') })
    const media = Context.latest.elements[0]
    set(media, 'readyState', 2)
    media.dispatchEvent(new Event('loadedmetadata'))
    media.dispatchEvent(new Event('waiting'))
    set(media, 'seeking', true)
    media.currentTime = 4
    media.dispatchEvent(new Event('seeking'))
    media.dispatchEvent(new Event('timeupdate'))
    expect(state().presence).toBe('idle')
    set(media, 'seeking', false)
    media.dispatchEvent(new Event('seeked'))
    media.dispatchEvent(new Event('timeupdate'))
    expect(state().presence).toBe('idle')
    media.currentTime = 4.25
    media.dispatchEvent(new Event('timeupdate'))
    expect(state().playing).toEqual([cue('a').url])
    set(media, 'paused', true)
    media.dispatchEvent(new Event('pause'))
    media.currentTime = 5
    media.dispatchEvent(new Event('timeupdate'))
    expect(state().presence).toBe('idle')
    mixer.dispose()
  })

  it('does not stack or restart a one-shot on resync, and lets a sounding one finish through connection silence', async () => {
    const { mixer, setTime, state } = setup()
    await mixer.unlock()
    const shot = Context.latest.elements[2]
    const one = cue('c', { loop: false })
    mixer.apply({ ...emptyAudioSlots, one_shot: one })
    sounding(shot)
    shot.currentTime = 2
    setTime(T0 + 12_000)
    mixer.apply({ ...emptyAudioSlots, one_shot: one }, { resync: true })
    expect(shot.currentTime).toBe(2)
    const two = cue('d', { loop: false, started_at: new Date(T0 + 12_000).toISOString() })
    mixer.apply({ ...emptyAudioSlots, one_shot: two })
    sounding(shot)
    expect(shot.currentTime).toBe(0)
    expect(state().playing).toEqual([two.url])
    mixer.silence()
    expect(shot.paused).toBe(false)
    set(shot, 'ended', true)
    shot.dispatchEvent(new Event('ended'))
    expect(shot.hasAttribute('src')).toBe(false)
    mixer.dispose()
  })

  it.each([true, false])('rejoins a phone-paused one-shot on a fresh snapshot (pause event received: %s)', async (pauseEventReceived) => {
    const { mixer, setTime, state } = setup()
    await mixer.unlock()
    const shot = Context.latest.elements[2]
    const one = cue('c', { loop: false })
    const slots = { ...emptyAudioSlots, one_shot: one }
    mixer.apply(slots)
    sounding(shot)
    shot.currentTime = 2
    shot.pause()
    if (pauseEventReceived) shot.dispatchEvent(new Event('pause'))
    setTime(T0 + 19_000) // Exactly one second remains.
    const attempts = played.length
    mixer.apply(slots)
    expect(played).toHaveLength(attempts)
    expect(state().playing).toEqual([])
    mixer.apply(slots, { resync: true })
    expect(played).toHaveLength(attempts + 1)
    expect(shot.currentTime).toBe(19)
    expect(state().playing).toEqual([])
    shot.dispatchEvent(new Event('playing'))
    expect(state().playing).toEqual([one.url])
    mixer.dispose()
  })

  it.each([0, 4])('drops a paused one-shot with under a second left at readyState %i', async (readyState) => {
    const { mixer, setTime, state } = setup()
    await mixer.unlock()
    const shot = Context.latest.elements[2]
    const slots = { ...emptyAudioSlots, one_shot: cue('c', { loop: false }) }
    mixer.apply(slots)
    sounding(shot)
    shot.pause()
    shot.dispatchEvent(new Event('pause'))
    set(shot, 'readyState', readyState)
    setTime(T0 + 19_001)
    const attempts = played.length
    mixer.apply(slots, { resync: true })
    expect(played).toHaveLength(attempts)
    expect(shot.hasAttribute('src')).toBe(false)
    expect(state().status).toBe('idle')
    mixer.dispose()
  })

  it('declines remote URLs and skips a late one-shot without touching the media network', async () => {
    const { mixer, state, setTime } = setup()
    await mixer.unlock()
    mixer.apply({ ...emptyAudioSlots, ambience: cue('a', { url: 'https://outside.invalid/table/assets/' + 'a'.repeat(32) }) })
    expect(state().status).toBe('error')
    expect(played).toHaveLength(3)
    setTime(T0 + 19_100)
    mixer.apply({ ...emptyAudioSlots, one_shot: cue('b', { loop: false }) }, { resync: true })
    expect(played).toHaveLength(3)
    mixer.dispose()
  })

  it('reports media permission errors and a suspended context without claiming playback', async () => {
    const { mixer, state } = setup()
    await mixer.unlock()
    fail = 'permission'
    mixer.apply({ ...emptyAudioSlots, ambience: cue('a') })
    await next()
    expect(state()).toMatchObject({ needsGesture: true, presence: 'pending', status: 'error', playing: [] })
    fail = undefined
    await mixer.unlock()
    sounding(Context.latest.elements[0])
    expect(state().presence).toBe('listening')
    Context.latest.state = 'suspended'
    Context.latest.onstatechange?.()
    expect(state()).toMatchObject({ playing: [], needsGesture: true, presence: 'pending' })
    mixer.clear()
    expect(Context.latest.elements.every(el => !el.hasAttribute('src'))).toBe(true)
    mixer.dispose()
  })

  it('retains the page volume and mute across cleared playback and a new authorized cue', async () => {
    const { mixer, state } = setup()
    await mixer.unlock()
    const [master] = Context.latest.gains
    mixer.setVolume(0.4)
    mixer.setMuted(true)
    mixer.apply({ ...emptyAudioSlots, ambience: cue('a') })
    mixer.clear()
    mixer.apply({ ...emptyAudioSlots, ambience: cue('b') })
    expect(state()).toMatchObject({ accepted: true, presence: 'muted' })
    mixer.setMuted(false)
    expect(master.gain.setValueAtTime).toHaveBeenLastCalledWith(0.4, 0)
    expect(played.filter(url => url.includes('audio-unlock'))).toHaveLength(3)
    mixer.dispose()
  })
})

describe('the player sound gate', () => {
  it('uses the prompt on a pending cue, transfers focus and reports only audible handles', async () => {
    const root = document.createElement('div')
    document.body.append(root)
    const report = vi.fn()
    const controls = mountTableAudio(root, report, () => T0)
    const [accept, mute] = root.querySelectorAll('button')
    controls.show(true, emptyAudioSlots)
    expect(root.querySelector('section')?.dataset.variant).toBe('banner')
    const a = cue('a')
    controls.show(true, { ...emptyAudioSlots, ambience: a })
    expect(root.querySelector('section')?.dataset.variant).toBe('prompt')
    expect(accept.textContent).toContain('Your GM is playing something')
    expect(report).toHaveBeenLastCalledWith('pending', [])
    accept.focus()
    accept.click()
    await next()
    expect(controls.accepted).toBe(true)
    expect(document.activeElement).toBe(mute)
    expect(report).toHaveBeenLastCalledWith('idle', [])
    sounding(Context.latest.elements[0])
    expect(report).toHaveBeenLastCalledWith('listening', [a.url])
    mute.click()
    expect(mute.getAttribute('aria-pressed')).toBe('true')
    expect(report).toHaveBeenLastCalledWith('muted', [])
    controls.hide()
    expect(report).toHaveBeenLastCalledWith('absent', [])
    controls.show(true, emptyAudioSlots)
    expect(controls.accepted).toBe(true)
    controls.reset()
    controls.show(true, emptyAudioSlots)
    expect(controls.accepted).toBe(false)
    expect(accept.hidden).toBe(false)
    controls.dispose()
    expect(root.childElementCount).toBe(0)
  })

  it('shows a recoverable error and keeps mute available when the activation itself fails', async () => {
    const root = document.createElement('div')
    const controls = mountTableAudio(root, vi.fn(), () => T0)
    controls.show(true, emptyAudioSlots)
    const [accept, mute] = root.querySelectorAll('button')
    fail = 'primer'
    accept.click()
    await next()
    expect(root.querySelector('[role=status]')?.textContent).toBe("Your browser can't play this sound")
    expect(mute.hidden).toBe(false)
    fail = undefined
    accept.click()
    await next()
    expect(controls.accepted).toBe(true)
    controls.dispose()
  })
})
