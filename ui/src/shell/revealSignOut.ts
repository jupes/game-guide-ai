/**
 * revealSignOut -- what the Login screen says when the session was lost while the table could
 * still see something (agent-forge-harness-1kg.7.3 PR-2, REVEAL-16): `The table can still see
 * <title>`.
 *
 * Memory only. `RevealProvider` sets it from the titles it holds at the moment a request comes
 * back 401, and `signIn` (every path: Login, Signup) clears it on a successful sign-in. A title is GM-private text (X-7), so it
 * is shown on the GM's own sign-in page and never stored, sent or logged.
 */

import { useSyncExternalStore } from 'react'
import { REVEAL_COPY } from '../gm/revealCopy'
import { Emitter } from './tableSessionApi'

class RevealSignOutNotice extends Emitter {
  text: string | null = null

  /** The titles the table could still see; none means nothing to say. */
  set(titles: readonly string[]): void {
    this.text = titles.length === 0 ? null : REVEAL_COPY.stillSees(titles)
    this.emit()
  }

  clear(): void {
    if (this.text === null) return
    this.text = null
    this.emit()
  }

  getSnapshot = (): string | null => this.text
}

export const revealSignOutNotice = new RevealSignOutNotice()

export function useRevealSignOutNotice(): string | null {
  return useSyncExternalStore(revealSignOutNotice.subscribe, revealSignOutNotice.getSnapshot)
}
