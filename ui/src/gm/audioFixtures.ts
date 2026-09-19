/**
 * audioFixtures — shared sample data for the audio stories and tests, so a
 * story and its unit test describe the same cue.
 *
 * Every value here is contract-shaped (`CueSchema`, the presence frame), and
 * `audioFixtures.test.ts` parses them to prove it. Nothing here is imported by
 * application code.
 */

import { CONTRACT_VERSION } from './contracts'
import type { Cue, CueKind } from './contracts'
import type { GuestPresence, ParticipantPresence, PresenceSummary } from './audioHelpers'

const CREATED = '2026-09-18T19:04:00Z'

export function makeCue(overrides: Partial<Cue> = {}): Cue {
  return {
    schema_version: CONTRACT_VERSION,
    cue_id: 'cue_tidewarden',
    campaign_id: 'camp_saltmarsh',
    title: 'Tidewarden Chant',
    kind: 'ambience',
    asset_id: 'asset_tidewarden',
    duration_ms: 190_000,
    archived: false,
    created_at: CREATED,
    updated_at: CREATED,
    ...overrides,
  }
}

/** The ambience the GM loops under a scene. */
export const AMBIENCE_CUE: Cue = makeCue()

/** A one-shot: never loops, at most 30 s (AUDIO-3, AUDIO-26). */
export const ONE_SHOT_CUE: Cue = makeCue({
  cue_id: 'cue_thunder',
  title: 'Thunderclap',
  kind: 'one_shot',
  asset_id: 'asset_thunder',
  duration_ms: 4_000,
})

export function makeParticipant(
  alias: string,
  audio: ParticipantPresence['audio'],
  participantId = `p_${alias.toLowerCase()}`,
): ParticipantPresence {
  return { participant_id: participantId, alias, audio }
}

export function makeGuests(overrides: Partial<GuestPresence> = {}): GuestPresence {
  return { connected: 0, listening: 0, muted: 0, pending: 0, ...overrides }
}

/** Two listening, one muted, one enrolled player who has not tapped (AUDIO-21). */
export const PRESENCE_MIXED: PresenceSummary = {
  participants: [
    makeParticipant('Ondrey', 'listening'),
    makeParticipant('Mira', 'listening'),
    makeParticipant('Sel', 'muted'),
    makeParticipant('Gorath', 'pending'),
  ],
  guests: makeGuests({ connected: 1, pending: 1 }),
}

/** Nobody waiting — the sentence is just the two counts. */
export const PRESENCE_SETTLED: PresenceSummary = {
  participants: [makeParticipant('Ondrey', 'listening'), makeParticipant('Sel', 'muted')],
  guests: makeGuests({ connected: 1, listening: 1 }),
}

export const CUE_KIND_TITLES: Record<CueKind, string> = {
  ambience: AMBIENCE_CUE.title,
  one_shot: ONE_SHOT_CUE.title,
}
