/**
 * Rail pins (1kg.3.3) — RAIL-10, RAIL-11, RAIL-12, and AE-11's arithmetic.
 *
 * The privacy rule is tested as hard as the behaviour: what reaches
 * `localStorage` is tool ids and nothing else (X-7), and every access is
 * wrapped, so a device that refuses storage still gets a working rail.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { REGISTRY, RAIL_LIMIT, toolAvailability } from './registry'
import { canPin, defaultPins, movePin, pinsStorageKey, readPins, togglePin, usePins, writePins } from './pins'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })
const KEY = pinsStorageKey('gm-1')

// jsdom 29's own localStorage does not expose .clear() in every runner
// configuration, so the suite brings its own — the same stub theme.test.tsx
// uses, and the only way to make "storage refuses" a testable state.
function makeLocalStorageStub() {
  let store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value
    },
    removeItem: (key: string) => {
      delete store[key]
    },
    clear: () => {
      store = {}
    },
    get length() {
      return Object.keys(store).length
    },
    key: (index: number) => Object.keys(store)[index] ?? null,
    keys: () => Object.keys(store),
  }
}

let storage: ReturnType<typeof makeLocalStorageStub>

beforeEach(() => {
  storage = makeLocalStorageStub()
  vi.stubGlobal('localStorage', storage)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('readPins — RAIL-11', () => {
  it('absent means the registry defaults', () => {
    expect(readPins('gm-1')).toEqual([...REGISTRY.default_pinned])
  })

  it('an empty list means the GM deliberately emptied the rail', () => {
    storage.setItem(KEY, '[]')
    expect(readPins('gm-1')).toEqual([])
  })

  it('keeps the stored order', () => {
    storage.setItem(KEY, JSON.stringify(['rules', 'npc', 'loot']))
    expect(readPins('gm-1')).toEqual(['rules', 'npc', 'loot'])
  })

  it('drops unknown ids and never back-fills', () => {
    storage.setItem(KEY, JSON.stringify(['npc', 'ninth-tool', 'loot']))
    expect(readPins('gm-1')).toEqual(['npc', 'loot'])
  })

  it('drops a repeat rather than pinning it twice', () => {
    storage.setItem(KEY, JSON.stringify(['npc', 'npc', 'loot']))
    expect(readPins('gm-1')).toEqual(['npc', 'loot'])
  })

  it(`never returns more than ${RAIL_LIMIT}`, () => {
    storage.setItem(KEY, JSON.stringify(REGISTRY.tools.map((t) => t.id)))
    expect(readPins('gm-1')).toHaveLength(RAIL_LIMIT)
  })

  it('keeps a pin that has since been disabled — it holds its place and still counts (RAIL-10)', () => {
    storage.setItem(KEY, JSON.stringify(['npc', 'portrait', 'loot']))
    const pins = readPins('gm-1')
    expect(pins).toEqual(['npc', 'portrait', 'loot'])
    // Availability is what renders it disabled, not the pin list.
    expect(DEFAULTS.portrait.enabled).toBe(false)
  })

  it('falls back to the defaults on a truncated payload', () => {
    storage.setItem(KEY, '["npc",')
    expect(readPins('gm-1')).toEqual([...REGISTRY.default_pinned])
  })

  it('falls back to the defaults when the payload is not a list', () => {
    storage.setItem(KEY, '{"npc":true}')
    expect(readPins('gm-1')).toEqual([...REGISTRY.default_pinned])
  })

  it('falls back to the defaults when the device refuses to read (private mode)', () => {
    vi.spyOn(storage, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    expect(readPins('gm-1')).toEqual([...REGISTRY.default_pinned])
  })

  it('is scoped per account, so the next GM on this browser sees their own rail (RAIL-12)', () => {
    storage.setItem(pinsStorageKey('gm-1'), JSON.stringify(['rules']))
    expect(readPins('gm-1')).toEqual(['rules'])
    expect(readPins('gm-2')).toEqual([...REGISTRY.default_pinned])
  })
})

describe('writePins — RAIL-12 and X-7', () => {
  it('stores tool ids and nothing else', () => {
    writePins('gm-1', ['npc', 'recap'])
    expect(storage.getItem(KEY)).toBe('["npc","recap"]')
  })

  it('never writes anything but the pin key for this account', () => {
    writePins('gm-1', ['npc'])
    expect(storage.keys()).toEqual([KEY])
  })

  it('reports a refusal instead of throwing when the quota is spent', () => {
    vi.spyOn(storage, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(writePins('gm-1', ['npc'])).toBe(false)
  })
})

describe('canPin and togglePin — RAIL-10, RAIL-11', () => {
  it('refuses a sixth pin', () => {
    const full = [...REGISTRY.default_pinned]
    expect(full).toHaveLength(RAIL_LIMIT)
    expect(canPin(full, 'recap', ALL_ENABLED)).toBe(false)
    expect(togglePin(full, 'recap', ALL_ENABLED)).toEqual(full)
  })

  it('refuses a duplicate', () => {
    expect(canPin(['npc'], 'npc', ALL_ENABLED)).toBe(false)
  })

  it('refuses a tool that is disabled right now (AE-11, AE-12)', () => {
    expect(canPin(['npc'], 'portrait', DEFAULTS)).toBe(false)
    expect(togglePin(['npc'], 'portrait', DEFAULTS)).toEqual(['npc'])
  })

  it('pins an enabled tool at the end', () => {
    expect(togglePin(['npc'], 'recap', ALL_ENABLED)).toEqual(['npc', 'recap'])
  })

  it('unpins a disabled tool — unpinning is always allowed (AE-11)', () => {
    expect(togglePin(['npc', 'portrait'], 'portrait', DEFAULTS)).toEqual(['npc'])
  })
})

describe('movePin — SLASH-15, reordering is never drag-only', () => {
  it('moves a pin up', () => {
    expect(movePin(['npc', 'monster', 'loot'], 'monster', -1)).toEqual(['monster', 'npc', 'loot'])
  })

  it('moves a pin down', () => {
    expect(movePin(['npc', 'monster', 'loot'], 'monster', 1)).toEqual(['npc', 'loot', 'monster'])
  })

  it('does nothing at either end, or for a tool that is not pinned', () => {
    expect(movePin(['npc', 'monster'], 'npc', -1)).toEqual(['npc', 'monster'])
    expect(movePin(['npc', 'monster'], 'monster', 1)).toEqual(['npc', 'monster'])
    expect(movePin(['npc', 'monster'], 'recap', -1)).toEqual(['npc', 'monster'])
  })
})

describe('defaultPins', () => {
  it('is the registry default, copied so a caller cannot edit the registry', () => {
    const pins = defaultPins()
    expect(pins).toEqual([...REGISTRY.default_pinned])
    pins.push('recap')
    expect(REGISTRY.default_pinned).toHaveLength(RAIL_LIMIT)
  })
})

// ── usePins ──────────────────────────────────────────────────────────────────

function PinProbe({ userId }: { userId: string }) {
  const { pins, savePins, persisted } = usePins(userId)
  return (
    <div>
      <span data-testid="pins">{pins.join(',')}</span>
      <span data-testid="persisted">{String(persisted)}</span>
      <button type="button" onClick={() => savePins(['recap', 'rules'])}>
        save
      </button>
      <button type="button" onClick={() => savePins(['recap', 'recap', 'rules'])}>
        save-messy
      </button>
    </div>
  )
}

describe('usePins — a durable rail order (RAIL-12)', () => {
  it('starts from the stored pins, without an effect round-trip', () => {
    storage.setItem(KEY, JSON.stringify(['rules', 'npc']))
    render(<PinProbe userId="gm-1" />)
    expect(screen.getByTestId('pins')).toHaveTextContent('rules,npc')
  })

  it('saves to the device and reports the new order at once', async () => {
    render(<PinProbe userId="gm-1" />)
    await userEvent.click(screen.getByRole('button', { name: 'save' }))
    expect(screen.getByTestId('pins')).toHaveTextContent('recap,rules')
    expect(storage.getItem(KEY)).toBe('["recap","rules"]')
  })

  it('normalises whatever it is handed before storing it — a repeat is dropped', async () => {
    render(<PinProbe userId="gm-1" />)
    await userEvent.click(screen.getByRole('button', { name: 'save-messy' }))
    expect(storage.getItem(KEY)).toBe('["recap","rules"]')
  })

  it('re-reads when the account changes, so one browser never mixes two rails', () => {
    storage.setItem(pinsStorageKey('gm-1'), JSON.stringify(['rules']))
    storage.setItem(pinsStorageKey('gm-2'), JSON.stringify(['recap']))
    const { rerender } = render(<PinProbe userId="gm-1" />)
    expect(screen.getByTestId('pins')).toHaveTextContent('rules')
    rerender(<PinProbe userId="gm-2" />)
    expect(screen.getByTestId('pins')).toHaveTextContent('recap')
  })

  it('still changes the rail when the device refuses to store it, and says so', async () => {
    vi.spyOn(storage, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    render(<PinProbe userId="gm-1" />)
    await userEvent.click(screen.getByRole('button', { name: 'save' }))
    expect(screen.getByTestId('pins')).toHaveTextContent('recap,rules')
    expect(screen.getByTestId('persisted')).toHaveTextContent('false')
  })
})
