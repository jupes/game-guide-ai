/**
 * audioGain — the GM's PRIVATE preview gain stage and the one number this
 * feature is allowed to remember.
 *
 *  - AUDIO-30  every gain change goes through a Web Audio gain stage, never
 *              `element.volume`, which iOS does not let a script set. The
 *              context is created lazily, from a user gesture, and the factory
 *              is injected so tests can supply a fake.
 *  - AUDIO-5   the slider is the GM's own volume and is remembered per device.
 *  - AUDIO-7   nothing here touches the table: the stage exists only for the
 *              GM's monitor and preview.
 *  - X-7       only a NUMBER is stored. No title, no filename, no cue id.
 *
 * The table client's playback engine is `1kg.8.7`'s and is not this module.
 */

import * as React from 'react'
import { clampUnit } from './audioHelpers'

/** The one key this feature writes. Its name carries no GM-private text (X-7). */
export const PREVIEW_VOLUME_KEY = 'aetheril.gm.audio.previewVolume'
/** The handoff's resting volume, kept so a first load sounds the same. */
export const DEFAULT_PREVIEW_VOLUME = 0.66

/** Injected in tests; `window.localStorage` in a browser. */
export type VolumeStore = Pick<Storage, 'getItem' | 'setItem'>

function defaultStore(): VolumeStore | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    // Storage access throws outright in some privacy modes.
    return null
  }
}

/**
 * AUDIO-5. Anything that is not a finite number in 0–1 — a missing key, a
 * hostile string someone typed into devtools, a throwing store — falls back to
 * the default rather than reaching the gain stage.
 */
export function readPreviewVolume(store: VolumeStore | null = defaultStore()): number {
  if (!store) return DEFAULT_PREVIEW_VOLUME
  try {
    const raw = store.getItem(PREVIEW_VOLUME_KEY)
    if (raw === null) return DEFAULT_PREVIEW_VOLUME
    const value = Number(raw)
    if (!Number.isFinite(value) || value < 0 || value > 1) return DEFAULT_PREVIEW_VOLUME
    return value
  } catch {
    return DEFAULT_PREVIEW_VOLUME
  }
}

/** Writes the number, or silently does nothing — a full or blocked store is not an error the GM should see. */
export function writePreviewVolume(volume: number, store: VolumeStore | null = defaultStore()): void {
  if (!store || !Number.isFinite(volume)) return
  try {
    store.setItem(PREVIEW_VOLUME_KEY, String(clampUnit(volume)))
  } catch {
    // A quota error or a blocked store loses the preference, nothing else.
  }
}

// ── The gain stage (AUDIO-30) ────────────────────────────────────────────────

export interface PreviewGainStage {
  /** True once an `AudioContext` exists. */
  readonly started: boolean
  /** True when the platform refused to give us a context; callers degrade quietly. */
  readonly unavailable: boolean
  /**
   * Build the graph and resume the context. Call this from a user gesture and
   * nowhere else — creating a context outside one is what makes iOS refuse.
   */
  unlock(): void
  /** The GM's own gain, 0–1. Remembered until the stage is unlocked. */
  setGain(value: number): void
  /** Disconnect the graph and close the context. Idempotent. */
  dispose(): void
}

export interface PreviewGainStageOptions {
  /** The GM's own preview element. Its `volume` is never written (AUDIO-30). */
  media: HTMLMediaElement
  /** Injected so a test can hand in a fake; the platform's constructor by default. */
  createContext?: () => AudioContext
  /** Applied as soon as the stage is unlocked. */
  initialGain?: number
}

function platformContext(): AudioContext {
  return new AudioContext()
}

/**
 * A three-node graph — media element source → gain → destination — built once,
 * on the first `unlock()`. Nothing is created before then, so importing this
 * module never starts audio (no autoplay).
 */
export function createPreviewGainStage({
  media,
  createContext = platformContext,
  initialGain = DEFAULT_PREVIEW_VOLUME,
}: PreviewGainStageOptions): PreviewGainStage {
  let context: AudioContext | null = null
  let source: MediaElementAudioSourceNode | null = null
  let gain: GainNode | null = null
  let disposed = false
  let unavailable = false
  let wanted = clampUnit(initialGain)

  function build(): void {
    if (context || disposed || unavailable) return
    try {
      const made = createContext()
      const node = made.createGain()
      const from = made.createMediaElementSource(media)
      from.connect(node)
      node.connect(made.destination)
      context = made
      source = from
      gain = node
    } catch {
      // No Web Audio here. The element still plays; it just plays at its own
      // gain, and we never fall back to `element.volume` (AUDIO-30).
      unavailable = true
    }
  }

  return {
    get started() {
      return context !== null
    },
    get unavailable() {
      return unavailable
    },
    unlock() {
      if (disposed) return
      build()
      if (!context || !gain) return
      gain.gain.value = wanted
      if (context.state === 'suspended') void context.resume()
    },
    setGain(value: number) {
      if (disposed) return
      wanted = clampUnit(value)
      if (gain) gain.gain.value = wanted
    },
    dispose() {
      if (disposed) return
      disposed = true
      try {
        source?.disconnect()
        gain?.disconnect()
        void context?.close()
      } catch {
        // A context the platform already tore down; nothing left to release.
      }
      source = null
      gain = null
      context = null
    },
  }
}

export interface UsePreviewGainOptions {
  /** The GM's own preview element, once the owner has one. */
  media: HTMLMediaElement | null
  volume: number
  muted: boolean
  /** Read once, on mount; a fake in tests. */
  createContext?: () => AudioContext
}

/**
 * Owns a stage for as long as a preview element exists, applies the GM's
 * volume and local mute to it, and disposes it on unmount or when the element
 * is swapped — so a closed cue card never leaves an `AudioContext` open.
 *
 * The returned object's identity never changes: it is a facade over the stage
 * the effect owns, which keeps the hook free of any state update and makes it
 * safe to hand straight to `AudioCue`, whose play gesture unlocks it.
 *
 * The owner (`1kg.8.5`) holds the element and the asset URL; this hook only
 * ever touches gain, never `element.volume` and never the table (AUDIO-7).
 */
export function usePreviewGain({ media, volume, muted, createContext }: UsePreviewGainOptions): PreviewGainStage {
  const stage = React.useRef<PreviewGainStage | null>(null)
  const wanted = React.useRef(clampUnit(muted ? 0 : volume))
  // Read once, on mount: an element may be handed to `createMediaElementSource`
  // only ever once, so a changed factory must not rebuild a live graph.
  const factory = React.useRef(createContext)

  const [facade] = React.useState<PreviewGainStage>(() => ({
    get started() {
      return stage.current?.started ?? false
    },
    get unavailable() {
      return stage.current?.unavailable ?? false
    },
    unlock() {
      stage.current?.unlock()
    },
    setGain(value: number) {
      wanted.current = clampUnit(value)
      stage.current?.setGain(wanted.current)
    },
    dispose() {
      stage.current?.dispose()
      stage.current = null
    },
  }))

  React.useEffect(() => {
    if (!media) return
    const made = createPreviewGainStage({
      media,
      createContext: factory.current,
      initialGain: wanted.current,
    })
    stage.current = made
    return () => {
      made.dispose()
      stage.current = null
    }
  }, [media])

  React.useEffect(() => {
    facade.setGain(muted ? 0 : volume)
  }, [facade, muted, volume])

  return facade
}
