import primer from './audio-unlock.wav?no-inline'

export type MixerSlot = 'ambience' | 'one_shot'
export interface MixerCue {
  url: string
  seq?: number
  started_at: string
  start_offset_ms: number
  duration_ms: number
  loop: boolean
}
export type MixerSlots = Readonly<Record<MixerSlot, MixerCue | null>>
export const emptyAudioSlots: MixerSlots = { ambience: null, one_shot: null }
export type AudioPresence = 'pending' | 'idle' | 'listening' | 'muted' | 'absent'
export interface MixerState {
  accepted: boolean
  muted: boolean
  status: 'idle' | 'loading' | 'error'
  needsGesture: boolean
  presence: Exclude<AudioPresence, 'absent'>
  playing: readonly string[]
}

// The caller supplies server-adjusted wall time. The media clock is in seconds.
export function cuePosition(cue: MixerCue, nowMs: number, lateOneShot = false): number | null {
  const elapsed = Math.max(0, nowMs - Date.parse(cue.started_at)) + cue.start_offset_ms
  if (!Number.isFinite(elapsed) || !Number.isFinite(cue.duration_ms) || cue.duration_ms <= 0) return null
  if (!cue.loop && (elapsed >= cue.duration_ms || (lateOneShot && cue.duration_ms - elapsed < 1000))) return null
  return (cue.loop ? elapsed % cue.duration_ms : elapsed) / 1000
}

interface Envelope { from: number; to: number; start: number; end: number }
const level = (value: number): Envelope => ({ from: value, to: value, start: 0, end: 0 })

// Retarget an in-flight ramp at its actual level, including on browsers
// without cancelAndHoldAtTime.
function ramp(param: AudioParam, previous: Envelope, now: number, to: number, duration: number): Envelope {
  const fraction = now >= previous.end ? 1 : Math.max(0, (now - previous.start) / (previous.end - previous.start))
  const from = previous.from + (previous.to - previous.from) * fraction
  param.cancelScheduledValues(now)
  param.setValueAtTime(duration === 0 ? to : from, now)
  if (duration > 0) param.linearRampToValueAtTime(to, now + duration)
  return { from, to, start: now, end: now + duration }
}

interface Voice {
  slot: MixerSlot
  media: HTMLAudioElement
  source: MediaElementAudioSourceNode
  gain: GainNode
  envelope: Envelope
  cue: MixerCue | null
  phase: 'idle' | 'loading' | 'playing' | 'error'
  needsGesture: boolean
  retired: boolean
  stopTimer?: number
  stopAt?: number
  seek: boolean
  late: boolean
}

function sameCue(a: MixerCue | null, b: MixerCue | null): boolean {
  return a === b || (a !== null && b !== null && a.url === b.url && a.seq === b.seq &&
    a.started_at === b.started_at && a.start_offset_ms === b.start_offset_ms &&
    a.duration_ms === b.duration_ms && a.loop === b.loop)
}

function mediaUrl(path: string, document: Document): string | null {
  try {
    const url = new URL(path, document.baseURI)
    const allowed = /^\/table\/assets\/[A-Za-z0-9_-]{32}$|^\/campaigns\/[A-Za-z0-9_-]+\/assets\/[A-Za-z0-9_-]+$/
    return url.origin === document.location.origin && !url.username && !url.password && allowed.test(url.pathname)
      ? url.href : null
  } catch { return null }
}

/** Three reusable media elements stream MP3s: two for an ambience crossfade
 * and one for the replaceable one-shot. No cue is fetched before consent. */
export function createAudioMixer(
  document: Document, onChange: (state: MixerState) => void = () => {}, now: () => number = Date.now,
) {
  const browser = document.defaultView!
  const voices: Voice[] = []
  let context: AudioContext | undefined
  let master: GainNode | undefined
  let ambience: GainNode | undefined
  let duck = level(1)
  let slots: MixerSlots = emptyAudioSlots
  let accepted = false
  let muted = false
  let volume = 1
  let unlocking = false
  let unlockError = false
  let resuming = false
  let disposed = false

  function publish(): void {
    if (disposed) return
    const active = voices.filter(v => v.cue && !v.retired)
    const running = context?.state === 'running'
    const playing = running && !muted && volume > 0
      ? active.filter(v => v.phase === 'playing' && !v.media.paused && !v.media.ended).map(v => v.cue!.url)
      : []
    const needsGesture = unlockError || active.some(v => v.needsGesture) || (accepted && !running && active.length > 0)
    const status = unlockError || active.some(v => v.phase === 'error') ? 'error'
      : unlocking || active.some(v => v.phase === 'loading') ? 'loading' : 'idle'
    const presence = muted ? 'muted' : playing.length ? 'listening' : !accepted || needsGesture ? 'pending' : 'idle'
    onChange({ accepted, muted, needsGesture, status, presence, playing })
  }

  function updateDuck(): void {
    if (!context || !ambience) return
    const sounding = voices.some(v => v.slot === 'one_shot' && v.phase === 'playing' && !v.retired)
    const to = sounding ? 0.35 : 1
    if (to !== duck.to) duck = ramp(ambience.gain, duck, context.currentTime, to, sounding ? 0.25 : 0.6)
  }

  function clear(voice: Voice): void {
    browser.clearTimeout(voice.stopTimer)
    voice.stopAt = undefined
    voice.cue = null
    voice.retired = false
    voice.needsGesture = false
    voice.phase = 'idle'
    voice.media.pause()
    voice.media.removeAttribute('src')
    voice.media.load() // Abort outstanding ranges and release the decoded media.
    voice.envelope = ramp(voice.gain.gain, voice.envelope, context!.currentTime, 0, 0)
    updateDuck()
  }

  function retire(voice: Voice, fade: number): void {
    if (!voice.cue) return
    const stopAt = context!.currentTime + fade
    if (voice.retired && voice.stopAt !== undefined && voice.stopAt <= stopAt) return
    browser.clearTimeout(voice.stopTimer)
    voice.retired = true
    voice.stopAt = stopAt
    updateDuck()
    if (!fade || voice.phase !== 'playing') { clear(voice); return }
    voice.envelope = ramp(voice.gain.gain, voice.envelope, context!.currentTime, 0, fade)
    voice.stopTimer = browser.setTimeout(() => { clear(voice); publish() }, fade * 1000)
  }

  function position(voice: Voice): boolean {
    if (!voice.cue || !voice.seek || voice.media.readyState < 1) return true
    const offset = cuePosition(voice.cue, now(), voice.late && voice.slot === 'one_shot')
    if (offset === null) { clear(voice); return false }
    voice.media.currentTime = offset
    voice.seek = false
    return true
  }

  function play(voice: Voice): void {
    const cue = voice.cue
    void voice.media.play().catch((reason: unknown) => {
      if (disposed || !cue || voice.cue !== cue || voice.retired) return
      voice.phase = 'error'
      voice.needsGesture = reason instanceof DOMException && reason.name === 'NotAllowedError'
      updateDuck()
      publish()
    })
  }

  function makeVoice(slot: MixerSlot): Voice {
    const media = document.createElement('audio')
    media.preload = 'none'
    const source = context!.createMediaElementSource(media)
    const gain = context!.createGain()
    gain.gain.value = 0
    source.connect(gain)
    gain.connect(slot === 'ambience' ? ambience! : master!)
    const voice: Voice = { slot, media, source, gain, envelope: level(0), cue: null, phase: 'idle',
      retired: false, needsGesture: false, seek: false, late: false }
    media.addEventListener('loadedmetadata', () => {
      if (voice.cue && !voice.retired) { position(voice); publish() }
    })
    media.addEventListener('playing', () => {
      if (!voice.cue || voice.retired || media.paused || !position(voice)) return
      voice.phase = 'playing'
      voice.needsGesture = false
      updateDuck()
      publish()
    })
    for (const event of ['waiting', 'pause']) media.addEventListener(event, () => {
      if (!voice.cue || voice.retired || (event === 'pause' && !media.paused)) return
      if (voice.phase !== 'error') voice.phase = 'loading'
      updateDuck()
      publish()
    })
    media.addEventListener('ended', () => {
      if (!voice.cue || !media.ended) return
      clear(voice)
      publish()
    })
    media.addEventListener('error', () => {
      if (!voice.cue || voice.retired || !media.error) return
      voice.phase = 'error'
      updateDuck()
      publish()
    })
    return voice
  }

  function apply(next: MixerSlots, { resync = false } = {}): void {
    const prior = slots
    slots = next
    if (disposed || !accepted || !context) { publish(); return }
    if (resync && context.state !== 'running' && !resuming && !unlocking) {
      resuming = true
      void context.resume().catch(() => {}).finally(() => { resuming = false; publish() })
    }
    for (const slot of ['ambience', 'one_shot'] as const) {
      const cue = next[slot]
      const pool = voices.filter(v => v.slot === slot)
      const current = pool.find(v => v.cue && !v.retired)
      if (!cue) {
        for (const voice of pool) retire(voice, 0.3)
        continue
      }
      if (current && sameCue(current.cue, cue)) {
        if (resync && slot === 'ambience') {
          current.seek = true
          if (position(current) && current.media.paused && current.phase !== 'error') play(current)
        }
        continue
      }
      // An ended/skipped shot must not replay on every unrelated update.
      if (!current && sameCue(prior[slot], cue) && !resync) continue
      const fade = slot === 'ambience' && current !== undefined ? 1.5 : 0
      if (current) retire(current, fade)
      if (cuePosition(cue, now(), resync && slot === 'one_shot') === null) continue
      const voice = pool.find(v => !v.cue) ?? pool.find(v => v !== current) ?? pool[0]
      if (voice.cue) clear(voice)
      voice.cue = cue
      voice.seek = true
      voice.late = resync
      voice.phase = 'loading'
      const url = mediaUrl(cue.url, document)
      if (!url) {
        voice.phase = 'error'
        continue
      }
      voice.media.loop = slot === 'ambience' && cue.loop
      voice.envelope = ramp(voice.gain.gain, voice.envelope, context.currentTime, 1, fade)
      voice.media.src = url
      play(voice)
    }
    publish()
  }

  function applyGain(): void {
    if (master && context) master.gain.setValueAtTime(muted ? 0 : volume, context.currentTime)
    publish()
  }

  async function unlock(): Promise<void> {
    if (disposed || unlocking) return
    unlockError = false
    unlocking = true
    publish()
    let timer: number | undefined
    try {
      if (!context) {
        const create = browser.AudioContext ?? (browser as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
        if (!create) throw new Error('Web Audio unavailable')
        context = new create()
        master = context.createGain()
        master.connect(context.destination)
        ambience = context.createGain()
        ambience.connect(master)
        for (const slot of ['ambience', 'ambience', 'one_shot'] as const) voices.push(makeVoice(slot))
        context.onstatechange = publish
        applyGain()
      }
      // Call every activation-sensitive operation on the original click stack.
      // Safari grants autoplay per element, so subsequent cues reuse this pool.
      const tasks = [context.resume()]
      for (const voice of voices) {
        if (!accepted) {
          voice.media.loop = false
          voice.media.src = primer
          tasks.push(voice.media.play())
        } else if (voice.cue && !voice.retired && voice.phase === 'error') {
          voice.phase = 'loading'
          voice.needsGesture = false
          play(voice)
        }
      }
      const timeout = new Promise<never>((_, reject) => {
        timer = browser.setTimeout(() => reject(new Error('Audio activation timed out')), 6500)
      })
      await Promise.race([Promise.all(tasks), timeout])
      if (disposed) return
      if (context.state !== 'running') throw new Error('Audio context suspended')
      if (!accepted) {
        for (const voice of voices) clear(voice)
        accepted = true
      }
      apply(slots, { resync: true })
    } catch {
      if (!disposed) {
        unlockError = true
        if (!accepted) {
          for (const voice of voices) clear(voice)
          if (context && voices.length !== 3) {
            for (const voice of voices) { voice.source.disconnect(); voice.gain.disconnect() }
            voices.length = 0
            ambience?.disconnect()
            master?.disconnect()
            void context.close().catch(() => {})
            context = undefined
          }
        }
      }
    } finally {
      browser.clearTimeout(timer)
      unlocking = false
      publish()
    }
  }

  return {
    get accepted() { return accepted },
    unlock, apply,
    setMuted(value: boolean) { muted = value; applyGain() },
    setVolume(value: number) { volume = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0; applyGain() },
    silence() {
      const finishing = voices.some(v => v.slot === 'one_shot' && v.phase === 'playing' && !v.retired)
      apply({ ambience: null, one_shot: finishing ? slots.one_shot : null })
    },
    clear() {
      slots = emptyAudioSlots
      for (const voice of voices) clear(voice)
      publish()
    },
    dispose() {
      if (disposed) return
      disposed = true
      for (const voice of voices) { clear(voice); voice.source.disconnect(); voice.gain.disconnect() }
      ambience?.disconnect()
      master?.disconnect()
      if (context) { context.onstatechange = null; void context.close().catch(() => {}) }
    },
  }
}

export type AudioMixer = ReturnType<typeof createAudioMixer>
