import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { LocalStorageConversationStore, MemoryConversationStore } from './conversationStore'

// ── CP-F5.2 — ConversationStore behaviors (#20) ───────────────────────────────

describe('MemoryConversationStore', () => {
  let store: MemoryConversationStore

  beforeEach(() => {
    store = new MemoryConversationStore()
  })

  it('create returns a conversation with correct id, mode, title, createdAt', () => {
    const conv = store.create('sage')
    expect(conv.id).toBeTruthy()
    expect(typeof conv.id).toBe('string')
    expect(conv.mode).toBe('sage')
    expect(conv.title).toBe('New conversation')
    expect(conv.createdAt).toBeTruthy()
    // createdAt is a valid ISO string
    expect(() => new Date(conv.createdAt)).not.toThrow()
    expect(isNaN(new Date(conv.createdAt).getTime())).toBe(false)
  })

  it('list filters by mode — sage conversations do not appear under spell', () => {
    store.create('sage')
    store.create('spell')
    store.create('sage')

    const sageConvs = store.list('sage')
    const spellConvs = store.list('spell')

    expect(sageConvs).toHaveLength(2)
    expect(spellConvs).toHaveLength(1)
    expect(sageConvs.every((c) => c.mode === 'sage')).toBe(true)
    expect(spellConvs.every((c) => c.mode === 'spell')).toBe(true)
  })

  it('auto-title: firstPrompt sets the title', () => {
    const conv = store.create('sage', 'What is a basilisk?')
    expect(conv.title).toBe('What is a basilisk?')
  })

  it('auto-title: truncates at 40 chars', () => {
    const longPrompt = 'This is a very long prompt that exceeds the forty character limit for titles'
    const conv = store.create('sage', longPrompt)
    expect(conv.title.length).toBeLessThanOrEqual(40)
    expect(conv.title).toBe(longPrompt.slice(0, 40).trim())
  })

  it('auto-title: no firstPrompt yields "New conversation"', () => {
    const conv = store.create('rules')
    expect(conv.title).toBe('New conversation')
  })

  it('rename updates the title', () => {
    const conv = store.create('sage')
    store.rename(conv.id, 'Dragon Lore')
    const [updated] = store.list('sage')
    expect(updated.title).toBe('Dragon Lore')
  })

  it('remove deletes the conversation', () => {
    const conv = store.create('sage')
    expect(store.list('sage')).toHaveLength(1)
    store.remove(conv.id)
    expect(store.list('sage')).toHaveLength(0)
  })

  it('per-mode isolation: sage and spell conversations are separate', () => {
    const sage1 = store.create('sage', 'Sage question')
    const spell1 = store.create('spell', 'Spell question')
    const gm1 = store.create('gm', 'GM question')

    expect(store.list('sage')).toEqual([sage1])
    expect(store.list('spell')).toEqual([spell1])
    expect(store.list('gm')).toEqual([gm1])
    expect(store.list('rules')).toHaveLength(0)
  })

  it('rename does not affect other conversations', () => {
    const a = store.create('sage', 'First')
    const b = store.create('sage', 'Second')
    store.rename(a.id, 'Renamed')
    const list = store.list('sage')
    expect(list.find((c) => c.id === a.id)?.title).toBe('Renamed')
    expect(list.find((c) => c.id === b.id)?.title).toBe('Second')
  })

  it('remove does not affect other conversations', () => {
    const a = store.create('sage', 'First')
    const b = store.create('sage', 'Second')
    store.remove(a.id)
    const list = store.list('sage')
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe(b.id)
  })
})

// ── 02t.7 — LocalStorageConversationStore robustness ──────────────────────────
// jsdom 29 ships a localStorage that may not expose every method in the runner;
// use an in-memory stub so these tests are hermetic (mirrors theme.test.tsx).

function makeLocalStorageStub() {
  let store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => { store[key] = value },
    removeItem: (key: string) => { delete store[key] },
    clear: () => { store = {} },
    get length() { return Object.keys(store).length },
    key: (index: number) => Object.keys(store)[index] ?? null,
  }
}

describe('LocalStorageConversationStore', () => {
  let lsMock: ReturnType<typeof makeLocalStorageStub>
  let store: LocalStorageConversationStore

  beforeEach(() => {
    lsMock = makeLocalStorageStub()
    vi.stubGlobal('localStorage', lsMock)
    store = new LocalStorageConversationStore()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('persists conversations across instances (round-trips through localStorage)', () => {
    const conv = store.create('sage', 'What is a basilisk?')
    // A fresh instance reads the same backing store.
    const reloaded = new LocalStorageConversationStore()
    const list = reloaded.list('sage')
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe(conv.id)
    expect(list[0].title).toBe('What is a basilisk?')
  })

  it('list filters by mode', () => {
    store.create('sage')
    store.create('spell')
    expect(store.list('sage')).toHaveLength(1)
    expect(store.list('spell')).toHaveLength(1)
    expect(store.list('gm')).toHaveLength(0)
  })

  it('rename and remove persist', () => {
    const conv = store.create('sage', 'Original')
    store.rename(conv.id, 'Renamed')
    expect(new LocalStorageConversationStore().list('sage')[0].title).toBe('Renamed')
    store.remove(conv.id)
    expect(new LocalStorageConversationStore().list('sage')).toHaveLength(0)
  })

  it('load() tolerates corrupt JSON without throwing', () => {
    lsMock.setItem('rag-chat:conversations', '{not valid json')
    expect(() => store.list('sage')).not.toThrow()
    expect(store.list('sage')).toEqual([])
  })

  it('create() does not throw when the write fails (quota exceeded) and warns instead', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    lsMock.setItem = () => {
      throw new DOMException('quota exceeded', 'QuotaExceededError')
    }

    // The operation must not crash the caller (no silent unhandled exception).
    expect(() => store.create('sage', 'overflow')).not.toThrow()
    // The failure is surfaced, not swallowed silently.
    expect(warn).toHaveBeenCalled()
  })
})
