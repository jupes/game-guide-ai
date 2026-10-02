/**
 * A per-width `window.matchMedia` for jsdom tests (agent-forge-harness-1kg.6.3).
 *
 * `matchMediaStub` answers every query with one shared boolean, which can say
 * "narrow" or "wide" but cannot say "medium" (matches the 1024 query and not
 * the 768 one). This stub holds a viewport width and evaluates the shell's
 * range queries, `(width < Npx)`, against it, so a test names a width
 * (375, 768, 900, 1023, 1024, 1280) and gets the layout a browser would.
 *
 * Only the `(width < Npx)` form is understood, because it is the only form the
 * shell queries (breakpoints.ts). Any other query throws, so a new query
 * cannot silently evaluate to "does not match".
 *
 * Install it in `beforeEach` and restore it in `afterEach`, never in
 * `test-setup.ts`: jsdom's missing `matchMedia` is what keeps every other test
 * on the wide layout.
 */

type ChangeListener = (event: MediaQueryListEvent) => void

export interface MatchMediaWidthStub {
  /** Move the viewport to `width`, and notify every listener whose answer changed. */
  setWidth(width: number): void
  /** The current viewport width. */
  readonly width: number
  /** Put back whatever `window.matchMedia` was before `install`. */
  restore(): void
  /** Every query string passed to `matchMedia`, in call order. */
  readonly queries: readonly string[]
  /** The `change` listeners currently subscribed, across every query. */
  listenerCount(): number
}

const RANGE_QUERY = /^\(width < (\d+)px\)$/

function evaluate(media: string, width: number): boolean {
  const match = RANGE_QUERY.exec(media)
  if (match === null) throw new Error(`matchMediaWidth cannot evaluate ${media}`)
  return width < Number(match[1])
}

export function installMatchMediaWidth(initialWidth: number): MatchMediaWidthStub {
  const hadOwn = Object.prototype.hasOwnProperty.call(window, 'matchMedia')
  const original = window.matchMedia
  const listeners = new Map<string, Set<ChangeListener>>()
  const queries: string[] = []
  let width = initialWidth

  window.matchMedia = (media: string): MediaQueryList => {
    queries.push(media)
    evaluate(media, width)
    const set = listeners.get(media) ?? new Set<ChangeListener>()
    listeners.set(media, set)
    const list = {
      media,
      get matches() {
        return evaluate(media, width)
      },
      addEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') set.add(listener)
      },
      removeEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') set.delete(listener)
      },
    }
    return list as unknown as MediaQueryList
  }

  return {
    setWidth(next: number) {
      const before = width
      width = next
      for (const [media, set] of listeners) {
        const was = evaluate(media, before)
        const now = evaluate(media, next)
        if (was === now) continue
        for (const listener of [...set]) listener({ matches: now, media } as MediaQueryListEvent)
      }
    },
    get width() {
      return width
    },
    restore() {
      if (hadOwn) {
        window.matchMedia = original
      } else {
        Reflect.deleteProperty(window, 'matchMedia')
      }
    },
    queries,
    listenerCount: () => [...listeners.values()].reduce((sum, set) => sum + set.size, 0),
  }
}
