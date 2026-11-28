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
import type { ToolId } from './contracts'
import { parseDraft } from './slash'

interface DraftSource {
  toolId: ToolId
  entryId: string
}

interface SavedDraft {
  text: string
  revision: number
  source: DraftSource | null
}

export interface DraftStore {
  /** This conversation's draft. `''` until something is typed. */
  draft: string
  source: DraftSource | null
  setDraft: (next: string, source?: DraftSource | null) => void
  /** Put back a rejected submission only if the GM has left this draft alone. */
  restoreDraft: (rejected: string, target?: string) => void
  /** Drop one conversation's draft — after a submit, or when it is deleted. */
  clearDraft: () => void
  /** A private attachment can create a thread while its GM is still composing. */
  moveToThread: (thread: string) => void
}

export function useConversationDrafts(conversationId: string | null): DraftStore {
  // A Map rather than a plain object: `null` (a conversation with no id yet) is a
  // key of its own with no sentinel string to collide with, and an id such as
  // `__proto__` or `constructor` is just another key.
  const [drafts, setDrafts] = React.useState<ReadonlyMap<string | null, SavedDraft>>(() => new Map())
  const saved = drafts.get(conversationId)
  const revision = saved?.revision ?? 0
  const source = saved?.source ?? null

  const setDraft = React.useCallback(
    (next: string, linked?: DraftSource | null) => {
      setDrafts((prev) => {
        const old = prev.get(conversationId)
        const parsed = old?.source ? parseDraft(next) : null
        const kept = parsed?.kind === 'command' && parsed.tool.id === old?.source?.toolId ? old.source : null
        const nextSource = linked === undefined ? kept : linked
        if ((old?.text ?? '') === next && (old?.source ?? null) === nextSource) return prev
        return new Map(prev).set(conversationId, { text: next, revision: (old?.revision ?? 0) + 1, source: nextSource })
      })
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

  const restoreDraft = React.useCallback((rejected: string, target = conversationId) => {
    setDrafts((prev) => {
      const old = prev.get(target)
      if (target === conversationId ? old?.revision !== revision + 1 || old.text !== '' : old !== undefined) return prev
      return new Map(prev).set(target, { text: rejected, revision: (old?.revision ?? 0) + 1, source })
    })
  }, [conversationId, revision, source])

  const moveToThread = React.useCallback((thread: string) => {
    setDrafts(prev => {
      const before = prev.get(conversationId)
      if (before === undefined || prev.has(thread)) return prev
      const next = new Map(prev)
      next.delete(conversationId)
      return next.set(thread, before)
    })
  }, [conversationId])

  return { draft: saved?.text ?? '', source, setDraft, restoreDraft, clearDraft, moveToThread }
}
