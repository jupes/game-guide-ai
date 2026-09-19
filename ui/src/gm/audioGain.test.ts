/**
 * audioGain — AUDIO-30 (every gain change goes through a Web Audio gain stage,
 * never `element.volume`), AUDIO-5 (the GM's own volume, remembered per device)
 * and X-7 (a NUMBER is the only thing this feature writes).
 *
 * The AudioContext is faked throughout: jsdom has none, which is exactly why
 * the factory is a parameter.
 */

import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  DEFAULT_PREVIEW_VOLUME,
  PREVIEW_VOLUME_KEY,
  createPreviewGainStage,
  readPreviewVolume,
  usePreviewGain,
  writePreviewVolume,
} from './audioGain'
import type { VolumeStore } from './audioGain'

// ── A fake Web Audio graph ───────────────────────────────────────────────────

interface FakeGraph {
  context: AudioContext
  gain: { gain: { value: number } }
  connections: string[]
  closed: boolean
  disconnects: string[]
  resumes: number
  sourcesMade: number
}

function makeGraph(state: 'running' | 'suspended' = 'running'): FakeGraph {
  const graph: Partial<FakeGraph> = {
    connections: [],
    disconnects: [],
    closed: false,
    resumes: 0,
    sourcesMade: 0,
  }
  const gain = {
    gain: { value: 1 },
    connect: () => graph.connections?.push('gain->destination'),
    disconnect: () => graph.disconnects?.push('gain'),
  }
  const context = {
    state,
    destination: {},
    createGain: () => gain,
    createMediaElementSource: () => {
      graph.sourcesMade = (graph.sourcesMade ?? 0) + 1
      return {
        connect: () => graph.connections?.push('source->gain'),
        disconnect: () => graph.disconnects?.push('source'),
      }
    },
    resume: () => {
      graph.resumes = (graph.resumes ?? 0) + 1
      return Promise.resolve()
    },
    close: () => {
      graph.closed = true
      return Promise.resolve()
    },
  }
  graph.gain = gain
  // A structural stand-in: only the members the stage actually uses exist.
  graph.context = context as unknown as AudioContext
  return graph as FakeGraph
}

/** An <audio> whose `volume` setter fails the test if anything writes it. */
function makeMedia(onVolumeWrite: () => void): HTMLMediaElement {
  const element = document.createElement('audio')
  Object.defineProperty(element, 'volume', {
    get: () => 1,
    set: () => onVolumeWrite(),
    configurable: true,
  })
  return element
}

function memoryStore(): VolumeStore & { map: Map<string, string> } {
  const map = new Map<string, string>()
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value)
    },
  }
}

// ── AUDIO-30 ─────────────────────────────────────────────────────────────────

describe('createPreviewGainStage (AUDIO-30)', () => {
  it('creates no AudioContext until a gesture unlocks it, so nothing autoplays', () => {
    const graph = makeGraph()
    const createContext = vi.fn(() => graph.context)
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext })

    stage.setGain(0.4)
    expect(createContext).not.toHaveBeenCalled()
    expect(stage.started).toBe(false)

    stage.unlock()
    expect(createContext).toHaveBeenCalledTimes(1)
    expect(stage.started).toBe(true)
  })

  it('wires media -> gain -> destination and applies the gain wanted before the unlock', () => {
    const graph = makeGraph()
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext: () => graph.context })

    stage.setGain(0.25)
    stage.unlock()

    expect(graph.connections).toEqual(['source->gain', 'gain->destination'])
    expect(graph.gain.gain.value).toBe(0.25)
  })

  it('never writes element.volume — the rule iOS forces', () => {
    const wrote = vi.fn()
    const graph = makeGraph()
    const stage = createPreviewGainStage({ media: makeMedia(wrote), createContext: () => graph.context })

    stage.unlock()
    stage.setGain(0.1)
    stage.setGain(0.9)
    stage.dispose()

    expect(wrote).not.toHaveBeenCalled()
  })

  it('clamps whatever it is handed', () => {
    const graph = makeGraph()
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext: () => graph.context })
    stage.unlock()

    stage.setGain(4)
    expect(graph.gain.gain.value).toBe(1)
    stage.setGain(-1)
    expect(graph.gain.gain.value).toBe(0)
    stage.setGain(Number.NaN)
    expect(graph.gain.gain.value).toBe(0)
  })

  it('resumes a suspended context on the gesture, and only then', () => {
    const graph = makeGraph('suspended')
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext: () => graph.context })

    expect(graph.resumes).toBe(0)
    stage.unlock()
    expect(graph.resumes).toBe(1)
  })

  it('builds the graph once however often it is unlocked', () => {
    const graph = makeGraph()
    const createContext = vi.fn(() => graph.context)
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext })

    stage.unlock()
    stage.unlock()
    stage.unlock()

    expect(createContext).toHaveBeenCalledTimes(1)
    expect(graph.sourcesMade).toBe(1)
  })

  it('disconnects and closes on dispose, and goes inert afterwards', () => {
    const graph = makeGraph()
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext: () => graph.context })

    stage.unlock()
    stage.dispose()

    expect(graph.disconnects.sort()).toEqual(['gain', 'source'])
    expect(graph.closed).toBe(true)
    expect(stage.started).toBe(false)

    stage.dispose()
    stage.unlock()
    stage.setGain(0.5)
    expect(graph.gain.gain.value).toBe(DEFAULT_PREVIEW_VOLUME)
  })

  it('degrades quietly when the platform has no Web Audio, and still never uses element.volume', () => {
    const wrote = vi.fn()
    const stage = createPreviewGainStage({
      media: makeMedia(wrote),
      createContext: () => {
        throw new Error('no AudioContext here')
      },
    })

    stage.unlock()

    expect(stage.unavailable).toBe(true)
    expect(stage.started).toBe(false)
    expect(wrote).not.toHaveBeenCalled()
    expect(() => stage.dispose()).not.toThrow()
  })

  it("reaches for the platform's own AudioContext when no factory is injected", () => {
    const graph = makeGraph()
    const made: number[] = []
    class FakeAudioContext {
      constructor() {
        made.push(1)
        return graph.context
      }
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    try {
      const stage = createPreviewGainStage({ media: makeMedia(() => {}) })
      stage.unlock()
      expect(made).toHaveLength(1)
      expect(stage.started).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('survives a context that throws while being torn down', () => {
    const graph = makeGraph()
    const context = {
      ...graph.context,
      close: () => {
        throw new Error('already gone')
      },
    } as unknown as AudioContext
    const stage = createPreviewGainStage({ media: makeMedia(() => {}), createContext: () => context })

    stage.unlock()
    expect(() => stage.dispose()).not.toThrow()
  })
})

// ── AUDIO-5 / X-7: the one number this feature remembers ─────────────────────

describe('preview volume memory (AUDIO-5, X-7)', () => {
  it('round-trips a number under one key', () => {
    const store = memoryStore()
    writePreviewVolume(0.42, store)

    expect([...store.map.keys()]).toEqual([PREVIEW_VOLUME_KEY])
    expect(store.map.get(PREVIEW_VOLUME_KEY)).toBe('0.42')
    expect(readPreviewVolume(store)).toBe(0.42)
  })

  it('stores a NUMBER and nothing else — no title, no id, no filename', () => {
    const store = memoryStore()
    writePreviewVolume(0.5, store)

    for (const [key, value] of store.map) {
      expect(key).not.toMatch(/tidewarden|chant|cue_|asset_/i)
      expect(Number.isFinite(Number(value))).toBe(true)
    }
  })

  it('clamps before it writes', () => {
    const store = memoryStore()
    writePreviewVolume(9, store)
    expect(store.map.get(PREVIEW_VOLUME_KEY)).toBe('1')
  })

  it('writes nothing at all for a value that is not a number', () => {
    const store = memoryStore()
    writePreviewVolume(Number.NaN, store)
    expect(store.map.size).toBe(0)
  })

  it('falls back to the default for a missing, junk or out-of-range value', () => {
    const store = memoryStore()
    expect(readPreviewVolume(store)).toBe(DEFAULT_PREVIEW_VOLUME)

    store.map.set(PREVIEW_VOLUME_KEY, 'loud')
    expect(readPreviewVolume(store)).toBe(DEFAULT_PREVIEW_VOLUME)

    store.map.set(PREVIEW_VOLUME_KEY, '4')
    expect(readPreviewVolume(store)).toBe(DEFAULT_PREVIEW_VOLUME)

    store.map.set(PREVIEW_VOLUME_KEY, '-1')
    expect(readPreviewVolume(store)).toBe(DEFAULT_PREVIEW_VOLUME)
  })

  it('survives a store that throws — a privacy mode must not break the card', () => {
    const hostile: VolumeStore = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }

    expect(readPreviewVolume(hostile)).toBe(DEFAULT_PREVIEW_VOLUME)
    expect(() => writePreviewVolume(0.3, hostile)).not.toThrow()
  })

  it('does nothing when there is no store at all', () => {
    expect(readPreviewVolume(null)).toBe(DEFAULT_PREVIEW_VOLUME)
    expect(() => writePreviewVolume(0.3, null)).not.toThrow()
  })

  it('reaches for the platform store when none is injected', () => {
    const store = memoryStore()
    vi.stubGlobal('localStorage', store)
    try {
      writePreviewVolume(0.31)
      expect(store.map.get(PREVIEW_VOLUME_KEY)).toBe('0.31')
      expect(readPreviewVolume()).toBe(0.31)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('survives a platform that throws on the mere mention of storage', () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('storage disabled')
      },
    })
    try {
      expect(readPreviewVolume()).toBe(DEFAULT_PREVIEW_VOLUME)
      expect(() => writePreviewVolume(0.3)).not.toThrow()
    } finally {
      if (original) Object.defineProperty(globalThis, 'localStorage', original)
      else Reflect.deleteProperty(globalThis, 'localStorage')
    }
  })
})

// ── The React lifetime (close/disconnect on unmount) ─────────────────────────

describe('usePreviewGain', () => {
  it('builds no stage until there is an element', () => {
    const createContext = vi.fn(() => makeGraph().context)
    const { result } = renderHook(() =>
      usePreviewGain({ media: null, volume: 0.5, muted: false, createContext }),
    )

    result.current.unlock()
    expect(createContext).not.toHaveBeenCalled()
    expect(result.current.started).toBe(false)
  })

  it('applies volume and the local mute through the stage', () => {
    const graph = makeGraph()
    const media = makeMedia(() => {})
    const { result, rerender } = renderHook(
      ({ volume, muted }: { volume: number; muted: boolean }) =>
        usePreviewGain({ media, volume, muted, createContext: () => graph.context }),
      { initialProps: { volume: 0.5, muted: false } },
    )

    result.current.unlock()
    expect(graph.gain.gain.value).toBe(0.5)

    rerender({ volume: 0.5, muted: true })
    expect(graph.gain.gain.value).toBe(0)

    rerender({ volume: 0.8, muted: false })
    expect(graph.gain.gain.value).toBe(0.8)
  })

  it('closes the context on unmount', () => {
    const graph = makeGraph()
    const media = makeMedia(() => {})
    const { result, unmount } = renderHook(() =>
      usePreviewGain({ media, volume: 0.5, muted: false, createContext: () => graph.context }),
    )

    result.current.unlock()
    expect(graph.closed).toBe(false)

    unmount()
    expect(graph.closed).toBe(true)
    expect(graph.disconnects.sort()).toEqual(['gain', 'source'])
  })

  it('tears down the old graph when the element is swapped', () => {
    const first = makeGraph()
    const second = makeGraph()
    const graphs = [first, second]
    const mediaA = makeMedia(() => {})
    const mediaB = makeMedia(() => {})

    const { result, rerender } = renderHook(
      ({ media }: { media: HTMLMediaElement }) =>
        usePreviewGain({
          media,
          volume: 0.5,
          muted: false,
          createContext: () => (graphs.shift() ?? second).context,
        }),
      { initialProps: { media: mediaA } },
    )

    result.current.unlock()
    rerender({ media: mediaB })

    expect(first.closed).toBe(true)
    result.current.unlock()
    expect(second.closed).toBe(false)
  })

  it('reports the stage it owns, and goes quiet once disposed', () => {
    const graph = makeGraph()
    const media = makeMedia(() => {})
    const { result } = renderHook(() =>
      usePreviewGain({ media, volume: 0.5, muted: false, createContext: () => graph.context }),
    )

    expect(result.current.started).toBe(false)
    expect(result.current.unavailable).toBe(false)

    result.current.unlock()
    expect(result.current.started).toBe(true)

    result.current.dispose()
    expect(graph.closed).toBe(true)
    expect(result.current.started).toBe(false)
    expect(result.current.unavailable).toBe(false)
  })

  it('keeps one identity across renders, so it is safe as a prop', () => {
    const graph = makeGraph()
    const media = makeMedia(() => {})
    const { result, rerender } = renderHook(
      ({ volume }: { volume: number }) =>
        usePreviewGain({ media, volume, muted: false, createContext: () => graph.context }),
      { initialProps: { volume: 0.5 } },
    )

    const first = result.current
    rerender({ volume: 0.7 })
    expect(result.current).toBe(first)
  })
})
