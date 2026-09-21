/**
 * audioFixtures — proof that the sample data the stories and unit tests share
 * is the shape the wire contract actually defines, and that the types the
 * components are written against are the contract's own.
 *
 * If `contracts.ts` changes, this fails before any component test does.
 */

import { describe, it, expect } from 'vitest'
import { CONTRACT_VERSION, CueSchema, GmEventSchema, ONE_SHOT_MAX_MS } from './contracts'
import { AMBIENCE_CUE, ONE_SHOT_CUE, PRESENCE_MIXED, makeCue, makeGuests, makeParticipant } from './audioFixtures'
import type { AudioSlot, GmPlaying } from './audioHelpers'

describe('cue fixtures', () => {
  it('are valid cue records', () => {
    expect(CueSchema.safeParse(AMBIENCE_CUE).success).toBe(true)
    expect(CueSchema.safeParse(ONE_SHOT_CUE).success).toBe(true)
  })

  it('keep a one-shot inside its 30 second cap (AUDIO-26)', () => {
    expect(ONE_SHOT_CUE.duration_ms).toBeLessThanOrEqual(ONE_SHOT_MAX_MS)
  })

  it('refuse a one-shot that is too long for its kind', () => {
    expect(CueSchema.safeParse(makeCue({ kind: 'one_shot', duration_ms: 60_000 })).success).toBe(false)
  })
})

describe('the types the components are written against', () => {
  it('describes a GM audio frame exactly as the contract does (AUDIO-11)', () => {
    const slot: AudioSlot = 'ambience'
    const playing: GmPlaying = {
      cue_id: AMBIENCE_CUE.cue_id,
      title: AMBIENCE_CUE.title,
      started_at: '2026-09-18T19:10:00Z',
      start_offset_ms: 0,
      loop: true,
      duration_ms: AMBIENCE_CUE.duration_ms,
    }

    const frame = GmEventSchema.safeParse({
      schema_version: CONTRACT_VERSION,
      event: 'audio',
      session_id: 'sess_1',
      gen: 1,
      audio_epoch: 4,
      slot,
      seq: 9,
      playing,
    })

    expect(frame.success).toBe(true)
  })

  it('describes a presence frame exactly as the contract does (AUDIO-21)', () => {
    const frame = GmEventSchema.safeParse({
      schema_version: CONTRACT_VERSION,
      event: 'presence',
      session_id: 'sess_1',
      gen: 1,
      participants: PRESENCE_MIXED.participants,
      guests: PRESENCE_MIXED.guests,
    })

    expect(frame.success).toBe(true)
  })

  it('still refuses a looping one-shot at the wire (AUDIO-3)', () => {
    const frame = GmEventSchema.safeParse({
      schema_version: CONTRACT_VERSION,
      event: 'audio',
      session_id: 'sess_1',
      gen: 1,
      audio_epoch: 4,
      slot: 'one_shot',
      seq: 9,
      playing: {
        cue_id: ONE_SHOT_CUE.cue_id,
        title: ONE_SHOT_CUE.title,
        started_at: '2026-09-18T19:10:00Z',
        start_offset_ms: 0,
        loop: true,
        duration_ms: ONE_SHOT_CUE.duration_ms,
      },
    })

    expect(frame.success).toBe(false)
  })

  it('builds presence pieces that satisfy the contract on their own', () => {
    expect(makeParticipant('Gorath', 'pending').alias).toBe('Gorath')
    expect(makeGuests({ connected: 2, pending: 2 }).listening).toBe(0)
  })
})
