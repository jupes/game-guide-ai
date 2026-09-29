import '@testing-library/jest-dom/vitest'

// `jsdom` ships no type declarations (no `@types/jsdom` dependency here),
// so this describes only the slice of vitest's runtime `globalThis.jsdom`
// (a `JSDOM` instance) that this shim reads — its DOM window.
interface JsdomGlobal {
  window: Pick<Window, 'localStorage' | 'sessionStorage'>
}

// Node 25+ ships its own global `localStorage` (and `sessionStorage`): the
// experimental, file-backed Web Storage API. Vitest's built-in jsdom
// environment only assigns jsdom's own globals for keys that are NOT
// already present on the Node global object, so once Node owns
// `localStorage`, jsdom's real Storage is silently shadowed by Node's
// object — which has no getItem/setItem/clear/key and undefined length
// when no `--localstorage-file` backing store is configured (as in a
// plain `vitest` run). `sessionStorage` happens to still work under Node's
// own in-memory implementation, but pin it too so both storages are
// jsdom's for storage-event/window-identity consistency, on every Node
// major and in CI alike (agent-forge-harness-itp).
const jsdomInstance = (globalThis as typeof globalThis & { jsdom?: JsdomGlobal }).jsdom

if (jsdomInstance) {
  for (const key of ['localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      enumerable: true,
      value: jsdomInstance.window[key],
    })
  }
}
