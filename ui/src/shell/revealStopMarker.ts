/**
 * revealStopMarker -- the opaque pending-stop marker (agent-forge-harness-1kg.7.3 PR-2, REVEAL-16).
 *
 * A Stop that the server has not acknowledged leaves one of these in `localStorage`, per account and
 * per device (the posture `gm/pins.ts` takes), so a reload does not leave the table looking at what the
 * GM tried to hide. It holds opaque ids, the epoch the Stop was pressed at and the command id the
 * courier minted: never a title, an alias or field text (X-7).
 *
 * It is replayed first on the next load, but only while the session it names is the live one and the
 * epoch has not moved on (`replayable`). A later, deliberate reveal advances the epoch, so a stale
 * marker is dropped instead of killing it. Reading never throws: a missing, unreadable or foreign value
 * is no marker.
 */

import { z } from 'zod'
import { OPAQUE_ID } from './workspaceFragment'
import type { RevealState } from '../gm/contracts'

const STORAGE_PREFIX = 'game-guide-ai:gm-pending-stops'
/** More markers than a GM can have documents live; a bound, not a feature. */
const MAX_MARKERS = 20

const opaque = z.string().regex(OPAQUE_ID)

const PendingStopSchema = z
  .object({
    campaignId: opaque,
    /** A document id, or `*` for Stop all. */
    documentId: z.union([opaque, z.literal('*')]),
    sessionId: opaque,
    epoch: z.number().int().min(0),
    commandId: opaque,
  })
  .strict()

export type PendingStop = z.infer<typeof PendingStopSchema>

export function pendingStopsKey(account: string): string {
  return `${STORAGE_PREFIX}:${account}`
}

export function readPendingStops(account: string): PendingStop[] {
  try {
    const raw = localStorage.getItem(pendingStopsKey(account))
    if (raw === null) return []
    const parsed = z.array(PendingStopSchema).max(MAX_MARKERS).safeParse(JSON.parse(raw))
    return parsed.success ? parsed.data : []
  } catch {
    return []
  }
}

function store(account: string, stops: readonly PendingStop[]): void {
  try {
    if (stops.length === 0) localStorage.removeItem(pendingStopsKey(account))
    else localStorage.setItem(pendingStopsKey(account), JSON.stringify(stops.slice(-MAX_MARKERS)))
  } catch {
    // Storage refused: the in-memory courier still retries for this page's life.
  }
}

const sameJob = (held: PendingStop, campaignId: string, documentId: string): boolean =>
  held.campaignId === campaignId && held.documentId === documentId

/** One marker per campaign and document: a second press replaces the first. */
export function writePendingStop(account: string, stop: PendingStop): void {
  const parsed = PendingStopSchema.safeParse(stop)
  if (!parsed.success) return
  store(account, [...readPendingStops(account).filter((held) => !sameJob(held, stop.campaignId, stop.documentId)), parsed.data])
}

export function clearPendingStop(account: string, campaignId: string, documentId: string): void {
  store(account, readPendingStops(account).filter((held) => !sameJob(held, campaignId, documentId)))
}

/** Whether a marker may be replayed into `picture`: the same session, and an epoch that has not moved on. */
export function replayable(stop: PendingStop, picture: RevealState | null): boolean {
  return picture !== null && picture.session_id === stop.sessionId && picture.reveal_epoch <= stop.epoch
}
