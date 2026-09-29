import { describe, expect, it } from 'vitest'

// agent-forge-harness-itp: Node 25 (and later) defines its own global
// `localStorage`, which vitest's jsdom environment leaves untouched because
// the key already exists on Node's global object. Without the shim in
// `test-setup.ts`, this object has no getItem/setItem/clear/key and
// `length` is `undefined`, so any test that scans storage (e.g.
// `appRouting.test.tsx`'s `storageValues(localStorage)`) passes vacuously
// on such a machine instead of exercising real Storage behaviour. These
// assertions pin a working, jsdom-backed Storage API regardless of which
// Node major is running the suite.
describe('test-setup: global Storage shim', () => {
  it.each([
    ['localStorage', () => localStorage],
    ['sessionStorage', () => sessionStorage],
  ])('%s has a working Storage API', (_name, getStorage) => {
    const storage = getStorage()

    expect(typeof storage.setItem).toBe('function')
    expect(typeof storage.getItem).toBe('function')
    expect(typeof storage.removeItem).toBe('function')
    expect(typeof storage.clear).toBe('function')
    expect(typeof storage.key).toBe('function')

    storage.clear()
    expect(storage.length).toBe(0)

    storage.setItem('agent-forge-harness-itp', 'ok')
    expect(storage.length).toBe(1)
    expect(storage.getItem('agent-forge-harness-itp')).toBe('ok')
    expect(storage.key(0)).toBe('agent-forge-harness-itp')

    storage.removeItem('agent-forge-harness-itp')
    expect(storage.getItem('agent-forge-harness-itp')).toBeNull()
    expect(storage.length).toBe(0)
  })

  it('is backed by jsdom, not Node\'s own global Storage', () => {
    const jsdomInstance = (globalThis as typeof globalThis & { jsdom?: { window: Window } })
      .jsdom

    expect(jsdomInstance).toBeDefined()
    expect(localStorage).toBe(jsdomInstance?.window.localStorage)
    expect(sessionStorage).toBe(jsdomInstance?.window.sessionStorage)
  })
})
