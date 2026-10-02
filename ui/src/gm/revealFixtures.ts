/**
 * Contract-shaped reveal pictures and seats (agent-forge-harness-1kg.7.3).
 *
 * Wire payloads run through the contract's own schemas, so a fixture that drifts
 * from the contract fails the suite rather than teaching a component a shape the
 * server never sends. Used by tests and stories only; nothing in the app imports it.
 */

import {
  RevealLiveSchema,
  RevealStateSchema,
  SeatSchema,
  type RevealLive,
  type RevealState,
  type Seat,
  type SeatStatus,
} from './contracts'

export const FIXTURE_SESSION_ID = 'ses_revealFixtureSession00001'

let disclosureCounter = 0

/** One live entry for `documentId`. Copies of one disclosure share `disclosure_id`. */
export function liveFixture(
  documentId: string,
  mask: readonly string[],
  extra: Partial<Record<keyof RevealLive, unknown>> = {},
): RevealLive {
  disclosureCounter += 1
  return RevealLiveSchema.parse({
    disclosure_id: `dis_fixtureDisclosure${String(disclosureCounter).padStart(6, '0')}`,
    document_id: documentId,
    type: 'npc',
    version: 2,
    mask: [...mask],
    stale_text: false,
    pending_delivery: false,
    ...extra,
  })
}

export interface PictureOptions {
  readonly sessionId?: string
  readonly epoch?: number
  readonly gen?: number
  /** What the table slot holds, or `null` / omitted for empty. */
  readonly table?: RevealLive | null
  /** One entry per participant slot: what that slot holds (`null` for empty). */
  readonly participants?: Readonly<Record<string, RevealLive | null>>
}

/** The GM's picture: the table slot always, then each participant slot named. */
export function pictureFixture(options: PictureOptions = {}): RevealState {
  const slots = [
    { slot: { kind: 'table' }, seq: 1, live: options.table ?? null },
    ...Object.entries(options.participants ?? {}).map(([participantId, live], index) => ({
      slot: { kind: 'participant', participant_id: participantId },
      seq: index + 2,
      live,
    })),
  ]
  return RevealStateSchema.parse({
    session_id: options.sessionId ?? FIXTURE_SESSION_ID,
    gen: options.gen ?? 1,
    reveal_epoch: options.epoch ?? 3,
    slots,
  })
}

/** A seat as the GM reads it. The alias is the only text a test cares about. */
export function seatFixture(participantId: string, alias: string, status: SeatStatus = 'confirmed'): Seat {
  return SeatSchema.parse({
    schema_version: 1,
    participant_id: participantId,
    alias,
    status,
    address: null,
    created_at: '2026-09-16T19:20:11Z',
    offered_at: null,
    offer_expires_at: null,
    accepted_at: null,
    confirmed_at: status === 'confirmed' ? '2026-09-16T19:25:00Z' : null,
    removed_at: status === 'removed' ? '2026-09-16T19:30:00Z' : null,
  })
}

export const BRANN = seatFixture('par_brann0000000000000001', 'Brann')
export const ANA = seatFixture('par_ana00000000000000001', 'Ana')
export const COLE = seatFixture('par_cole0000000000000001', 'Cole')
export const DEV = seatFixture('par_dev00000000000000001', 'Dev')
