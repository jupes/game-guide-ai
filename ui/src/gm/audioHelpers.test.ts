/**
 * audioHelpers — the record's wording and geometry, asserted directly.
 *
 * These are the rules the three components only compose: AUDIO-3, AUDIO-8,
 * AUDIO-10, AUDIO-17, AUDIO-21, AUDIO-22 and STATE-7.
 */

import { describe, it, expect } from 'vitest'
import {
  SEEK_ARROW_MS,
  SEEK_PAGE_MS,
  canPlayToTable,
  clampPositionMs,
  clampUnit,
  consentStatusMessage,
  cueStatusMessage,
  fallbackPeaks,
  formatClock,
  formatRemaining,
  isLoopable,
  kindLabel,
  kindSummary,
  nextSeekPositionMs,
  presenceSentence,
  seekValueText,
  stripStatusMessage,
  volumeValueText,
} from './audioHelpers'
import type { CueStatusInput } from './audioHelpers'
import { makeGuests, makeParticipant } from './audioFixtures'

const MINUS = String.fromCodePoint(0x2212)

// ── Time ─────────────────────────────────────────────────────────────────────

describe('formatClock', () => {
  it('writes minutes and padded seconds', () => {
    expect(formatClock(0)).toBe('0:00')
    expect(formatClock(42_000)).toBe('0:42')
    expect(formatClock(190_000)).toBe('3:10')
    expect(formatClock(600_000)).toBe('10:00')
  })

  it('adds an hour field only when there is one', () => {
    expect(formatClock(3_723_000)).toBe('1:02:03')
  })

  it('floors rather than rounding up, so a cue never reads as finished early', () => {
    expect(formatClock(41_999)).toBe('0:41')
  })

  it('reads 0:00 for anything unusable', () => {
    expect(formatClock(Number.NaN)).toBe('0:00')
    expect(formatClock(-5_000)).toBe('0:00')
    expect(formatClock(Number.POSITIVE_INFINITY)).toBe('0:00')
  })
})

describe('formatRemaining', () => {
  it('writes what is left with a true minus sign', () => {
    expect(formatRemaining(15_000, 190_000)).toBe(`${MINUS}2:55`)
  })

  it('never goes below zero', () => {
    expect(formatRemaining(200_000, 190_000)).toBe(`${MINUS}0:00`)
  })
})

// ── AUDIO-10: the slider says where it is, and moves by fixed steps ───────────

describe('seekValueText (AUDIO-10)', () => {
  it('reads "<position> of <duration>"', () => {
    expect(seekValueText(42_000, 190_000)).toBe('0:42 of 3:10')
  })
})

describe('volumeValueText', () => {
  it('reads a whole percentage', () => {
    expect(volumeValueText(0.66)).toBe('66%')
    expect(volumeValueText(0)).toBe('0%')
    expect(volumeValueText(1)).toBe('100%')
  })
})

describe('nextSeekPositionMs (AUDIO-10)', () => {
  const duration = 190_000

  it('moves the arrow keys 5 seconds in both orientations', () => {
    expect(SEEK_ARROW_MS).toBe(5_000)
    expect(nextSeekPositionMs('ArrowRight', 40_000, duration)).toBe(45_000)
    expect(nextSeekPositionMs('ArrowUp', 40_000, duration)).toBe(45_000)
    expect(nextSeekPositionMs('ArrowLeft', 40_000, duration)).toBe(35_000)
    expect(nextSeekPositionMs('ArrowDown', 40_000, duration)).toBe(35_000)
  })

  it('moves the Page keys 30 seconds', () => {
    expect(SEEK_PAGE_MS).toBe(30_000)
    expect(nextSeekPositionMs('PageUp', 40_000, duration)).toBe(70_000)
    expect(nextSeekPositionMs('PageDown', 40_000, duration)).toBe(10_000)
  })

  it('sends Home to the start and End to the end', () => {
    expect(nextSeekPositionMs('Home', 40_000, duration)).toBe(0)
    expect(nextSeekPositionMs('End', 40_000, duration)).toBe(duration)
  })

  it('clamps at both ends rather than wrapping', () => {
    expect(nextSeekPositionMs('ArrowLeft', 1_000, duration)).toBe(0)
    expect(nextSeekPositionMs('PageUp', 189_000, duration)).toBe(duration)
  })

  it('returns null for a key it does not own, so the component lets it through', () => {
    expect(nextSeekPositionMs('Tab', 10_000, duration)).toBeNull()
    expect(nextSeekPositionMs('Enter', 10_000, duration)).toBeNull()
    expect(nextSeekPositionMs(' ', 10_000, duration)).toBeNull()
  })
})

describe('clampPositionMs', () => {
  it('holds the position inside the cue', () => {
    expect(clampPositionMs(-1, 100)).toBe(0)
    expect(clampPositionMs(500, 100)).toBe(100)
    expect(clampPositionMs(Number.NaN, 100)).toBe(0)
    expect(clampPositionMs(50, Number.NaN)).toBe(0)
  })
})

describe('clampUnit', () => {
  it('holds a gain between 0 and 1', () => {
    expect(clampUnit(-2)).toBe(0)
    expect(clampUnit(2)).toBe(1)
    expect(clampUnit(Number.NaN)).toBe(0)
    expect(clampUnit(0.3)).toBe(0.3)
  })
})

describe('fallbackPeaks', () => {
  it('is deterministic for one seed, so a card never reflows', () => {
    expect(fallbackPeaks(46, 190_000)).toEqual(fallbackPeaks(46, 190_000))
  })

  it('differs between cues of different lengths', () => {
    expect(fallbackPeaks(8, 4_000)).not.toEqual(fallbackPeaks(8, 190_000))
  })

  it('stays inside the normalised range and survives nonsense', () => {
    for (const peak of fallbackPeaks(46, 190_000)) {
      expect(peak).toBeGreaterThanOrEqual(0)
      expect(peak).toBeLessThanOrEqual(1)
    }
    expect(fallbackPeaks(-4, Number.NaN)).toEqual([])
  })
})

// ── AUDIO-3, AUDIO-4: kinds ──────────────────────────────────────────────────

describe('kinds (AUDIO-3, AUDIO-4)', () => {
  it('only ambience can loop', () => {
    expect(isLoopable('ambience')).toBe(true)
    expect(isLoopable('one_shot')).toBe(false)
  })

  it('spells the immutable kind for people', () => {
    expect(kindLabel('ambience')).toBe('Ambience')
    expect(kindLabel('one_shot')).toBe('One-shot')
  })

  it('says what the loop flag means, and says nothing about looping a one-shot', () => {
    expect(kindSummary('ambience', true)).toBe('Ambience · loops')
    expect(kindSummary('ambience', false)).toBe('Ambience · plays once')
    expect(kindSummary('one_shot', true)).toBe('One-shot')
    expect(kindSummary('one_shot', false)).toBe('One-shot')
  })
})

// ── AUDIO-21: presence ───────────────────────────────────────────────────────

describe('presenceSentence (AUDIO-21)', () => {
  it("writes the record's sentence", () => {
    expect(
      presenceSentence({
        participants: [
          makeParticipant('Ondrey', 'listening'),
          makeParticipant('Mira', 'listening'),
          makeParticipant('Sel', 'muted'),
          makeParticipant('Gorath', 'pending'),
        ],
        guests: makeGuests({ connected: 1 }),
      }),
    ).toBe("2 listening · 1 muted · Gorath hasn't tapped in yet")
  })

  it('counts guests and never names them', () => {
    const sentence = presenceSentence({
      participants: [],
      guests: makeGuests({ connected: 1, pending: 1 }),
    })
    expect(sentence).toBe("0 listening · 0 muted · 1 guest hasn't tapped in yet")
  })

  it('pluralises a count of guests', () => {
    expect(
      presenceSentence({ participants: [], guests: makeGuests({ connected: 3, pending: 2 }) }),
    ).toBe("0 listening · 0 muted · 2 guests haven't tapped in yet")
  })

  it('joins named participants with a count of guests in one clause', () => {
    expect(
      presenceSentence({
        participants: [makeParticipant('Gorath', 'pending'), makeParticipant('Mira', 'pending')],
        guests: makeGuests({ connected: 2, pending: 2 }),
      }),
    ).toBe("0 listening · 0 muted · Gorath, Mira and 2 guests haven't tapped in yet")
  })

  it('folds guest listeners and mutes into the counts', () => {
    expect(
      presenceSentence({
        participants: [makeParticipant('Ondrey', 'listening')],
        guests: makeGuests({ connected: 3, listening: 2, muted: 1 }),
      }),
    ).toBe('3 listening · 1 muted')
  })

  it('drops an absent client from every count (AUDIO-22)', () => {
    expect(
      presenceSentence({
        participants: [makeParticipant('Ondrey', 'listening'), makeParticipant('Ghost', 'absent')],
        guests: makeGuests({ connected: 1, listening: 1 }),
      }),
    ).toBe('2 listening · 0 muted')
  })

  it('always leads with the two counts, even with nobody connected', () => {
    expect(presenceSentence({ participants: [], guests: makeGuests() })).toBe('0 listening · 0 muted')
  })
})

// ── STATE-7: one message per surface, and never a per-tick one ───────────────

describe('cueStatusMessage (STATE-7, §12.2)', () => {
  const base: CueStatusInput = {
    playability: 'ready',
    live: false,
    connection: 'connected',
    pushFailed: false,
    previewBlocked: false,
  }

  it('says nothing at rest', () => {
    expect(cueStatusMessage(base)).toBeNull()
  })

  it('reports a cue that is not playable yet, and one that never will be', () => {
    expect(cueStatusMessage({ ...base, playability: 'processing' })).toBe('Still processing…')
    expect(cueStatusMessage({ ...base, playability: 'failed' })).toBe("This file couldn't be processed")
  })

  it('reports a failed push, a blocked preview, a dropped stream and a live cue', () => {
    expect(cueStatusMessage({ ...base, pushFailed: true })).toBe("Couldn't play to the table")
    expect(cueStatusMessage({ ...base, previewBlocked: true })).toBe('Press play again to allow sound')
    expect(cueStatusMessage({ ...base, connection: 'reconnecting' })).toBe('Reconnecting…')
    expect(cueStatusMessage({ ...base, live: true })).toBe('Playing to the table')
  })

  it('puts the unplayable file ahead of everything else', () => {
    expect(
      cueStatusMessage({ ...base, playability: 'failed', pushFailed: true, live: true }),
    ).toBe("This file couldn't be processed")
  })

  it('takes no position, no seek and no volume, so no tick can change it', () => {
    // The type has five discrete fields and nothing continuous; this is the
    // structural half of "no announcement per playback tick" (AC 4).
    expect(Object.keys(base).sort()).toEqual([
      'connection',
      'live',
      'playability',
      'previewBlocked',
      'pushFailed',
    ])
  })
})

describe('stripStatusMessage (AUDIO-17)', () => {
  it('says Reconnecting… only while the stream is down', () => {
    expect(stripStatusMessage('reconnecting')).toBe('Reconnecting…')
    expect(stripStatusMessage('connected')).toBeNull()
  })
})

describe('consentStatusMessage (§12.2, audio (player))', () => {
  it("uses the record's player-side wording", () => {
    expect(consentStatusMessage('loading')).toBe('Loading sound…')
    expect(consentStatusMessage('error')).toBe("Your browser can't play this sound")
    expect(consentStatusMessage('idle')).toBeNull()
  })
})

// ── AUDIO-8, AUDIO-17: what each table control may do ────────────────────────

describe('canPlayToTable (AUDIO-17, §12.2)', () => {
  it('needs a playable cue and a live stream', () => {
    expect(canPlayToTable('ready', 'connected')).toBe(true)
    expect(canPlayToTable('ready', 'reconnecting')).toBe(false)
    expect(canPlayToTable('processing', 'connected')).toBe(false)
    expect(canPlayToTable('failed', 'connected')).toBe(false)
  })
})

