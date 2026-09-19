/**
 * Per-conversation drafts (1kg.3.3) — RAIL-25.
 *
 * "A draft, an armed tool and an armed edit belong to their conversation."
 * Today's composer holds ONE un-keyed draft, so `/npc <brief>` typed in the GM
 * channel is sent verbatim as a Sage question after a channel switch. Keying
 * the draft by conversation closes that, and returning to a conversation gives
 * its draft back.
 *
 * An armed tool needs no separate store: it is a reading of the draft text
 * (SLASH-6), so restoring the draft restores the armed row with it.
 *
 * **In memory only.** A draft is GM-private text, so it never reaches
 * `localStorage`, `sessionStorage`, a URL or a log (X-7, CANVAS-15). Losing a
 * draft on reload is the price; the record chose it deliberately.
 */

import * as React from 'react'

export interface DraftStore {
  /** This conversation's draft. `''` until something is typed. */
  draft: string
  setDraft: (next: string) => void
  /** Drop one conversation's draft — after a submit, or when it is deleted. */
  clearDraft: () => void
}

export function useConversationDrafts(conversationId: string | null): DraftStore {
  // A Map rather than a plain object: `null` (a conversation with no id yet) is a
  // key of its own with no sentinel string to collide with, and an id such as
  // `__proto__` or `constructor` is just another key.
  const [drafts, setDrafts] = React.useState<ReadonlyMap<string | null, string>>(() => new Map())

  const setDraft = React.useCallback(
    (next: string) => {
      setDrafts((prev) => (prev.get(conversationId) === next ? prev : new Map(prev).set(conversationId, next)))
    },
    [conversationId],
  )

  const clearDraft = React.useCallback(() => {
    setDrafts((prev) => {
      if (!prev.has(conversationId)) return prev
      const next = new Map(prev)
      next.delete(conversationId)
      return next
    })
  }, [conversationId])

  return { draft: drafts.get(conversationId) ?? '', setDraft, clearDraft }
}
