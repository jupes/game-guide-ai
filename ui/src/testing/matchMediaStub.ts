/**
 * A controllable `window.matchMedia` for jsdom tests (agent-forge-harness-0rn).
 *
 * jsdom has no `matchMedia`, which is why `useShellLayout` falls back to
 * 'wide' there and every existing test keeps today's layout. A test that
 * needs the narrow layout installs this in `beforeEach` and restores it in
 * `afterEach` — never in `test-setup.ts`, which would move every other test
 * off the fallback.
 *
 * Every query the code asks for shares one `matches` value; the test flips it
 * with `set()`, which dispatches `change` to each subscribed listener. The
 * stub records the query strings it was asked for, so a test can prove the
 * hook asks the right question, and counts live listeners, so a test can
 * prove the hook unsubscribes.
 */

type ChangeListener = (event: MediaQueryListEvent) => void

export interface MatchMediaStub {
  /** Flip every query's `matches`, and notify the subscribed listeners. */
  set(matches: boolean): void
  /** Put back whatever `window.matchMedia` was before `install`. */
  restore(): void
  /** Every query string passed to `matchMedia`, in call order. */
  readonly queries: readonly string[]
  /** The `change` listeners currently subscribed. */
  listenerCount(): number
}

export function installMatchMedia(initial: boolean): MatchMediaStub {
  const hadOwn = Object.prototype.hasOwnProperty.call(window, 'matchMedia')
  const original = window.matchMedia
  const listeners = new Set<ChangeListener>()
  const queries: string[] = []
  let matches = initial

  window.matchMedia = (media: string): MediaQueryList => {
    queries.push(media)
    const list = {
      media,
      get matches() {
        return matches
      },
      addEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') listeners.add(listener)
      },
      removeEventListener: (type: string, listener: ChangeListener) => {
        if (type === 'change') listeners.delete(listener)
      },
    }
    return list as unknown as MediaQueryList
  }

  return {
    set(next: boolean) {
      matches = next
      for (const listener of [...listeners]) {
        listener({ matches: next } as MediaQueryListEvent)
      }
    },
    restore() {
      if (hadOwn) {
        window.matchMedia = original
      } else {
        Reflect.deleteProperty(window, 'matchMedia')
      }
    },
    queries,
    listenerCount: () => listeners.size,
  }
}
