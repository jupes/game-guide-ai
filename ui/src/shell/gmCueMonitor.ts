import { readPreviewVolume } from '../gm/audioGain'
import { readCue } from '../gm/cueApi'
import { assetPath } from '../gm/documentFields'
import { createAudioMixer, emptyAudioSlots, type MixerCue, type MixerSlot } from '../table/audioMixer'
import type { AudioFrame } from './cueStream'

/** The GM hears the same two slots through the owner's authenticated media routes. */
export class GmCueMonitor {
  private mixer: ReturnType<typeof createAudioMixer> | null = null
  private frames: readonly AudioFrame[] = []
  private rendered = { ...emptyAudioSlots }
  private urls = new Map<string, string>()
  private pending = new Set<string>()
  private loading: AbortController | null = null
  private preview: HTMLMediaElement | null = null
  private muted = false
  private offset = 0
  private staleTimer?: ReturnType<typeof setTimeout>
  private live = false
  private connected = false
  private key = ''
  private readonly campaign: string
  private readonly request: typeof fetch

  constructor(campaign: string, request: typeof fetch) { this.campaign = campaign; this.request = request }

  start(): void {
    this.mixer = createAudioMixer(document, undefined, () => Date.now() + this.offset)
    this.mixer.setVolume(readPreviewVolume())
    this.mixer.setMuted(this.muted)
    document.addEventListener('click', this.activate, true)
    document.addEventListener('keydown', this.activate, true)
  }

  private activate = (event: Event): void => {
    if (!event.isTrusted || !this.mixer || this.mixer.accepted) return
    void this.mixer.unlock().then(() => this.update(this.frames, this.connected, this.live, this.offset, true))
  }

  setVolume(value: number): void { this.mixer?.setVolume(value) }
  get isMuted(): boolean { return this.muted }
  setMuted(value: boolean): void { this.muted = value; this.mixer?.setMuted(value) }

  previewStarted(media: HTMLMediaElement): void {
    this.preview = media
    this.rendered = { ...emptyAudioSlots }
    this.mixer?.clear()
  }

  previewEnded(media: HTMLMediaElement): void {
    if (this.preview !== media) return
    this.preview = null
    this.render(true)
  }

  update(frames: readonly AudioFrame[], connected: boolean, enabled: boolean, clockOffsetMs: number, forceResync = false): void {
    this.offset = clockOffsetMs
    const resync = forceResync || !this.connected
    this.connected = connected
    if (!enabled || !frames.some((frame) => frame.playing)) {
      clearTimeout(this.staleTimer)
      this.staleTimer = undefined
      this.live = false
      this.frames = []
      this.key = ''
      this.loading?.abort()
      this.pending.clear()
      this.urls.clear()
      this.rendered = { ...emptyAudioSlots }
      this.mixer?.clear()
      return
    }
    if (!connected) {
      if (!this.live) return
      this.staleTimer ??= setTimeout(() => {
        this.live = false
        this.staleTimer = undefined
        this.frames = []
        this.key = ''
        this.loading?.abort()
        this.pending.clear()
        this.urls.clear()
        this.rendered = { ...emptyAudioSlots }
        this.mixer?.silence()
      }, 10_000)
      return
    }
    clearTimeout(this.staleTimer)
    this.staleTimer = undefined
    this.live = true
    this.frames = frames
    if (!this.mixer?.accepted) return
    const key = frames.map((f) => `${f.gen}/${f.seq}/${f.playing?.cue_id ?? ''}`).join(',')
    if (key === this.key) {
      if (resync) this.render(true)
      return
    }
    this.key = key
    this.loading?.abort()
    const controller = new AbortController()
    this.loading = controller
    const playing = frames.flatMap((frame) => frame.playing ? [frame.playing.cue_id] : [])
    for (const id of this.urls.keys()) if (!playing.includes(id)) this.urls.delete(id)
    this.pending = new Set(playing.filter((id) => !this.urls.has(id)))
    this.render(resync)
    // Either slot may be independently stopped, unavailable or slow. Publish a
    // successful lookup as soon as it settles without waiting for its sibling.
    for (const id of this.pending) {
      void readCue(this.campaign, id, AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]), this.request)
        .then(cue => { if (!controller.signal.aborted) this.urls.set(id, assetPath(this.campaign, cue.asset_id)) })
        .catch(() => { if (!controller.signal.aborted) this.key = '' })
        .finally(() => {
          if (controller.signal.aborted) return
          this.pending.delete(id)
          this.render(resync)
        })
    }
  }

  private render(resync = false): void {
    if (!this.mixer || this.preview || !this.live) return
    const slots: Record<MixerSlot, MixerCue | null> = { ...emptyAudioSlots }
    for (const { slot, seq, playing } of this.frames) {
      const url = playing && this.urls.get(playing.cue_id)
      if (playing && url) slots[slot] = { ...playing, seq, url }
      else if (playing && this.pending.has(playing.cue_id)) slots[slot] = this.rendered[slot]
    }
    this.rendered = slots
    this.mixer.apply(slots, { resync })
  }

  dispose(): void {
    document.removeEventListener('click', this.activate, true)
    document.removeEventListener('keydown', this.activate, true)
    clearTimeout(this.staleTimer)
    this.staleTimer = undefined
    this.key = ''
    this.live = false
    this.preview = null
    this.frames = []
    this.rendered = { ...emptyAudioSlots }
    this.loading?.abort()
    this.pending.clear()
    this.urls.clear()
    this.mixer?.dispose()
    this.mixer = null
  }
}
