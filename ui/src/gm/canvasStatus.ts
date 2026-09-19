/**
 * Canvas reads (agent-forge-harness-1kg.6.1) — the pure pieces `CanvasPane` and
 * `VersionList` share: the header's aggregate save status (CANVAS-13), the
 * reveal state the server's live projection gives (REVEAL-13), version labels
 * and the client-side timestamp format (CANVAS-27).
 *
 * Nothing here touches the network, the DOM or storage. Document titles,
 * version summaries and field text are GM-private (X-7): they are arguments
 * that come back as rendered strings, never keys, ids or stored values.
 *
 * Kept out of the component files so that the mapping can be tested on its own
 * and so that `react-refresh` keeps its one-component-per-module rule.
 */

import type { DocumentVersion } from './contracts'
import { REGISTRY, documentTypeById } from './registry'

/** CANVAS-27: the history list pages 20 entries at a time, newest first. */
export const HISTORY_PAGE_SIZE = 20

// ── Save status (CANVAS-13) ──────────────────────────────────────────────────

/**
 * CANVAS-12 collapses every field's state into one of these for the header.
 * *Dirty* (`unsaved`) means any field that is editing with a changed value,
 * saving, in error or in conflict; the owner does that arithmetic, because the
 * pane owns no editing state (that is 1kg.6.5's).
 */
export type CanvasSaveStatus = 'saved' | 'saving' | 'unsaved' | 'error' | 'conflict'

export interface CanvasStatusRead {
  /** The one sentence the polite live region carries (CANVAS-13). */
  message: string
  /** True while the failure can be retried — STATE-2: no error is a dead end. */
  retryable: boolean
  /** Drives the tone only. The words above carry the meaning. */
  tone: 'idle' | 'progress' | 'error'
}

const SAVE_STATUS: Record<CanvasSaveStatus, { message: string; retryable: boolean; tone: CanvasStatusRead['tone'] }> = {
  saved: { message: 'Saved', retryable: false, tone: 'idle' },
  saving: { message: 'Saving…', retryable: false, tone: 'progress' },
  unsaved: { message: 'Unsaved changes', retryable: false, tone: 'progress' },
  error: { message: "Couldn't save", retryable: true, tone: 'error' },
  conflict: { message: 'Conflict — review', retryable: false, tone: 'error' },
}

/**
 * CANVAS-13's five readings. The failed save reads `Couldn't save — Retry`
 * exactly as the record writes it, but only while a retry is actually wired:
 * naming an action that is not there would be the dead end STATE-2 forbids.
 */
export function canvasStatusRead(status: CanvasSaveStatus, canRetry: boolean): CanvasStatusRead {
  const read = SAVE_STATUS[status]
  if (read.retryable && canRetry) return { ...read, message: `${read.message} — Retry` }
  return { ...read }
}

// ── Reveal state (REVEAL-13, REVEAL-14, REVEAL-8) ────────────────────────────

/**
 * What the server's live projection says about *this* document. The handoff's
 * `revealed: boolean` and the document payload's `data.revealed` are both
 * dropped (REVEAL-13): a boolean cannot say "I could not confirm", and the
 * client must never read that case as "nothing revealed".
 */
export type CanvasReveal =
  | { state: 'hidden' }
  | { state: 'unknown' }
  | {
      state: 'revealed'
      /** What is live, e.g. `portrait, name & voice`. */
      summary: string
      /** REVEAL-8: a revealed field's text differs in the latest version. */
      behindLatest?: boolean
    }

export interface CanvasRevealRead {
  /** Header text, or `null` while nothing is live (§12.2: indicators are absent). */
  message: string | null
  /** REVEAL-8's second line, when the table is pinned behind the latest text. */
  note: string | null
  /** The control that opens this document's sheet (REVEAL-14), or `null`. */
  openLabel: string | null
  /** Material Symbols ligature for that control when it renders icon-only. */
  openIcon: string
  /** REVEAL-6: Stop is always its own control. `null` when nothing can be live. */
  stopLabel: string | null
}

/**
 * REVEAL-14 reads the canvas header as **two controls**, never the handoff's
 * single pill where a stray click on the summary stopped the reveal. While the
 * state is unknown the header says so and keeps Stop reachable — narrowing
 * always wins (X-3) — but offers no widening, which would need an epoch this
 * client cannot vouch for (REVEAL-22).
 */
export function canvasRevealRead(reveal: CanvasReveal): CanvasRevealRead {
  if (reveal.state === 'hidden') {
    return { message: null, note: null, openLabel: 'Reveal to party', openIcon: 'visibility', stopLabel: null }
  }
  if (reveal.state === 'unknown') {
    return {
      message: 'Reveal state unknown — reconnecting',
      note: null,
      openLabel: null,
      openIcon: 'visibility',
      stopLabel: 'Stop showing',
    }
  }
  const behind = reveal.behindLatest === true
  return {
    message: `Revealed · ${reveal.summary}`,
    note: behind ? 'Table is seeing an earlier version' : null,
    openLabel: behind ? 'Update…' : 'Change what the table sees',
    openIcon: behind ? 'sync' : 'tune',
    stopLabel: 'Stop showing',
  }
}

// ── Document type (X-8) ──────────────────────────────────────────────────────

export interface DocumentTypeRead {
  label: string
  /** Material Symbols Rounded ligature. */
  icon: string
  /** False for a type this bundle's registry does not know. */
  known: boolean
}

/**
 * X-8: unknown is not NPC. A type the registry does not know renders a neutral
 * placeholder rather than the handoff's `documentType(id) || DOCUMENT_TYPES.npc`,
 * which would label a stranger's document an NPC dossier.
 */
export function documentTypeRead(id: string): DocumentTypeRead {
  const type = documentTypeById(id)
  if (type === undefined) return { label: 'Document', icon: 'description', known: false }
  return { label: type.label, icon: type.icon, known: true }
}

// ── Versions (CANVAS-27) ─────────────────────────────────────────────────────

/** The short form the handoff sent as `label`; this client derives it instead. */
export function versionLabel(versionNumber: number): string {
  return `v${versionNumber}`
}

const AUTHOR_LABEL: Record<DocumentVersion['author'], string> = { gm: 'You', assistant: 'Assistant' }

/** CANVAS-27: history rows name their author as `You` or `Assistant`. */
export function authorLabel(author: DocumentVersion['author']): string {
  return AUTHOR_LABEL[author]
}

/**
 * CANVAS-27: times are ISO timestamps on the wire and are formatted here, in
 * the reader's locale. The handoff's pre-formatted `"7:36 PM"` is rejected — it
 * carries neither a date nor a zone, so it is wrong for every reader who is not
 * in the writer's timezone and ambiguous the moment history spans a day.
 *
 * `null` means "this is not a timestamp I can format"; the caller then shows the
 * raw value rather than inventing a date.
 */
export function formatTimestamp(iso: string, locale?: string | string[]): string | null {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  try {
    return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(at)
  } catch {
    // An ill-formed language tag throws RangeError; a timestamp is not worth a
    // crashed pane.
    return null
  }
}

/**
 * A human label for a changed field. The registry is the authority; for a type
 * whose field labels have not landed yet (1kg.5.3) the key is humanised rather
 * than shown raw, which is the defect §16 records against the handoff's
 * `xpBudget` cells. Nothing here guesses at a *different* field.
 */
export function fieldLabel(key: string, documentType?: string): string {
  const type = documentType === undefined ? undefined : documentTypeById(documentType)
  const declared = type?.field_labels[key] ?? REGISTRY.common_field_labels[key]
  if (declared !== undefined) return declared
  const words = key.split('_').filter((word) => word.length > 0)
  if (words.length === 0) return key
  const humanised = words.join(' ')
  return humanised.charAt(0).toUpperCase() + humanised.slice(1)
}
