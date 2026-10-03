/**
 * revealPresentation -- what the canvas says about one document's reveal state
 * (agent-forge-harness-1kg.7.3, brief 5 and the Critic's item 15).
 *
 * A pure map from the store's picture to the header (`CanvasReveal`), the badge, the
 * live field keys and the field marker. The workspace indicator (PR-2) reads the same
 * picture through the same store, so chat and canvas agree by construction.
 *
 * REVEAL-13: until the picture is confirmed the canvas never says `GM ONLY`. While it
 * loads it says so (`CHECKING`); when it could not be read it says `unknown`, with no
 * widening offered (REVEAL-22). Stop stays reachable in both (X-3).
 */

import type { RevealState, Seat } from './contracts'
import type { CanvasReveal } from './canvasStatus'
import { REVEAL_COPY } from './revealCopy'
import { aliasList, liveOf, revealSummary, type LiveDocument } from './revealFields'
import type { DocumentType } from './registry'

/** What the store holds, as far as the canvas needs it. */
export interface RevealInputs {
  readonly status: 'idle' | 'loading' | 'none' | 'live' | 'unknown'
  readonly state: RevealState | null
  /** Document ids with an unacknowledged Stop (`*` for all). */
  readonly stopping: ReadonlySet<string>
  readonly stopFailed: boolean
}

export interface RevealPresentation {
  readonly reveal: CanvasReveal
  /** `undefined` is the document's own `GM ONLY`; `null` is "could not confirm"; otherwise the badge text. */
  readonly badge: string | null | undefined
  /** The field keys the table (or a chosen player) can see right now. */
  readonly fields: readonly string[]
  /** What each of those fields' marker says, or `undefined` for the default. */
  readonly note: string | undefined
  /** Whether the header offers to open the sheet. */
  readonly canOpen: boolean
  /** Whether the header offers Stop showing. */
  readonly canStop: boolean
}

/** No reveal controls and nothing claimed: a type this bundle does not know, which can never be revealed (X-8). */
export const NO_REVEAL_CONTROLS: RevealPresentation = {
  reveal: { state: 'hidden' },
  badge: undefined,
  fields: [],
  note: undefined,
  canOpen: false,
  canStop: false,
}

const HIDDEN: RevealPresentation = {
  reveal: { state: 'hidden' },
  badge: undefined,
  fields: [],
  note: undefined,
  canOpen: true,
  canStop: false,
}

/** `Brann`, or `2 players`: how the marker names who the copies are for. */
function whoOf(live: LiveDocument, seats: readonly Seat[]): string {
  if (live.audience.kind === 'table') return REVEAL_COPY.tableName
  return live.audience.ids.length >= 2 ? REVEAL_COPY.playersCount(live.audience.ids.length) : aliasList(live.audience.ids, seats)
}

function markerNote(live: LiveDocument, seats: readonly Seat[]): string | undefined {
  const who = whoOf(live, seats)
  if (live.allWaiting) return REVEAL_COPY.waitingToShow(who)
  return live.audience.kind === 'table' ? REVEAL_COPY.tableCanSee : REVEAL_COPY.whoCanSee(who)
}

export function revealPresentation(
  inputs: RevealInputs,
  documentId: string,
  type: DocumentType,
  seats: readonly Seat[],
): RevealPresentation {
  switch (inputs.status) {
    case 'idle':
      return { ...HIDDEN, canOpen: false }
    case 'loading':
      return { reveal: { state: 'loading' }, badge: REVEAL_COPY.checkingBadge, fields: [], note: undefined, canOpen: false, canStop: true }
    case 'unknown':
      return { reveal: { state: 'unknown' }, badge: null, fields: [], note: undefined, canOpen: false, canStop: true }
    case 'none':
    case 'live':
      break
  }
  const live = liveOf(inputs.state, documentId)
  const summary = revealSummary(inputs.state, documentId, type, seats)
  if (live === null || summary === null) return HIDDEN
  const stopping = inputs.stopping.has(documentId) || inputs.stopping.has('*')
  return {
    reveal: {
      state: 'revealed',
      summary: `${summary.fields} · ${summary.audience}`,
      behindLatest: live.staleText,
      waiting: summary.waiting,
      stopFailed: stopping && inputs.stopFailed,
    },
    badge: REVEAL_COPY.revealedBadge,
    fields: live.mask,
    note: markerNote(live, seats),
    canOpen: !stopping,
    canStop: true,
  }
}

// ── The workspace indicator (PR-2, REVEAL-14) ────────────────────────────────

/** One live document, as the workspace indicator lists it. */
export interface Projection {
  readonly documentId: string
  /** GM-private: rendered and nothing more (X-7). `a document` until it has been read. */
  readonly title: string
  /** `table`, `Brann`, or `2 players`. */
  readonly who: string
  /** A revealed field's text has changed since it was pinned (REVEAL-8). */
  readonly staleText: boolean
  /** Every copy is held until its seat is confirmed. */
  readonly waiting: boolean
}

/**
 * Every live document in the picture, once each, in slot order. Chat and canvas agree by construction:
 * the canvas header reads `revealPresentation`, the workspace header reads this, both from `useReveals()`.
 */
export function revealProjections(
  picture: RevealState | null,
  titles: ReadonlyMap<string, string>,
  seats: readonly Seat[],
): Projection[] {
  const ids = [...new Set(picture?.slots.flatMap((entry) => (entry.live === null ? [] : [entry.live.document_id])) ?? [])]
  return ids.flatMap((documentId) => {
    const live = liveOf(picture, documentId)
    if (live === null) return []
    return [
      {
        documentId,
        title: titles.get(documentId) ?? REVEAL_COPY.aDocument,
        who: live.audience.kind === 'table' ? REVEAL_COPY.indicatorTable : whoOf(live, seats),
        staleText: live.staleText,
        waiting: live.allWaiting,
      },
    ]
  })
}

/** `Revealed · Ondrey (table) · +1 more`. */
export function projectionSummary(projections: readonly Projection[]): string {
  const [first, ...rest] = projections
  if (first === undefined) return REVEAL_COPY.indicatorRevealed
  const head = `${REVEAL_COPY.indicatorRevealed} · ${first.title} (${first.who})`
  return rest.length === 0 ? head : `${head} · ${REVEAL_COPY.indicatorMore(rest.length)}`
}
