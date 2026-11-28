import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mountTableAudio } from './audioControls'
import { createAudioMixer, cuePosition, emptyAudioSlots, type MixerCue, type MixerState } from './audioMixer'

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
