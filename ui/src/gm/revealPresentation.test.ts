/**
 * revealPresentation.test.ts -- what the canvas header, badge and field markers say
 * for each reveal state (agent-forge-harness-1kg.7.3, brief 5, tests 30 to 32, and the
 * Critic's item 15). Chat and canvas agree by construction: both read the picture.
 */

import { describe, expect, it } from 'vitest'
import { documentTypeById, type DocumentType } from './registry'
import { ANA, BRANN, COLE, liveFixture, pictureFixture } from './revealFixtures'
import { revealPresentation, revealProjections, projectionSummary, type RevealInputs } from './revealPresentation'

const NPC = documentTypeById('npc') as DocumentType
const seats = [BRANN, ANA, COLE]
const NO_STOPS: ReadonlySet<string> = new Set()

function present(inputs: Partial<RevealInputs>, documentId = 'doc_a') {
  return revealPresentation(
    { status: 'none', state: null, stopping: NO_STOPS, stopFailed: false, ...inputs },
    documentId,
    NPC,
    seats,
  )
}

describe('the states before a picture is known', () => {
  it('loading says so, offers no Open, keeps Stop, and never claims GM ONLY or unknown', () => {
    const p = present({ status: 'loading' })
    expect(p.reveal).toEqual({ state: 'loading' })
    expect(p.badge).toBe('CHECKING')
    expect(p.canOpen).toBe(false)
    expect(p.canStop).toBe(true)
    expect(p.fields).toEqual([])
  })

  it('unknown offers no Open (REVEAL-22), keeps Stop, and its badge is the explicit null', () => {
    const p = present({ status: 'unknown' })
    expect(p.reveal).toEqual({ state: 'unknown' })
    // Mutation: defaulting the badge to GM ONLY on failure is `undefined`, not `null`.
    expect(p.badge).toBeNull()
    expect(p.canOpen).toBe(false)
    expect(p.canStop).toBe(true)
  })

  it('idle (no reveal scope) is hidden with no controls: nothing can be revealed without a provider', () => {
    const p = present({ status: 'idle' })
    expect(p.reveal).toEqual({ state: 'hidden' })
    expect(p.badge).toBeUndefined()
    expect(p.canOpen).toBe(false)
    expect(p.canStop).toBe(false)
  })
})

describe('a document the picture confirms is not live', () => {
  it('on no session: hidden, GM ONLY, Open offered, no Stop', () => {
    const p = present({ status: 'none' })
    expect(p.reveal).toEqual({ state: 'hidden' })
    expect(p.badge).toBeUndefined()
    expect(p.canOpen).toBe(true)
    expect(p.canStop).toBe(false)
  })

  it('on a session that holds another document: still hidden for this one', () => {
    const state = pictureFixture({ table: liveFixture('doc_other', ['name']) })
    const p = present({ status: 'live', state })
    expect(p.reveal).toEqual({ state: 'hidden' })
    expect(p.fields).toEqual([])
    expect(p.canOpen).toBe(true)
  })
})

describe('a live document (test 31)', () => {
  it('reads Revealed with the summary, flags the live fields, and says the table can see them', () => {
    const state = pictureFixture({ table: liveFixture('doc_a', ['name', 'qualifier', 'voice']) })
    const p = present({ status: 'live', state })
    expect(p.reveal).toEqual({
      state: 'revealed',
      summary: 'Name & voice, Qualifier · to the table',
      behindLatest: false,
      waiting: false,
      stopFailed: false,
    })
    expect(p.badge).toBe('REVEALED')
    expect(p.fields).toEqual(['name', 'qualifier', 'voice'])
    expect(p.note).toBe('The table can see this')
    expect(p.canOpen).toBe(true)
    expect(p.canStop).toBe(true)
  })

  it('stale text adds the earlier-version flag', () => {
    const state = pictureFixture({ table: liveFixture('doc_a', ['name'], { stale_text: true }) })
    expect(present({ status: 'live', state }).reveal).toMatchObject({ behindLatest: true })
  })

  it('names the player a private copy is for, and counts more than one', () => {
    const one = pictureFixture({ participants: { [BRANN.participant_id]: liveFixture('doc_a', ['name']) } })
    expect(present({ status: 'live', state: one }).note).toBe('Brann can see this')
    const disclosure = liveFixture('doc_a', ['name'])
    const two = pictureFixture({
      participants: { [BRANN.participant_id]: disclosure, [ANA.participant_id]: { ...disclosure } },
    })
    expect(present({ status: 'live', state: two }).note).toBe('2 players can see this')
  })

  it('a copy waiting on its seat does not say anyone can see it (Critic 15)', () => {
    const waiting = liveFixture('doc_a', ['name'], { pending_delivery: true })
    const only = pictureFixture({ participants: { [BRANN.participant_id]: waiting } })
    const p = present({ status: 'live', state: only })
    expect(p.note).toBe('Waiting to show Brann')
    expect(p.reveal).toMatchObject({ state: 'revealed', waiting: true })
    // Mutation: reading `can see this` for a waiting copy.
    expect(p.note).not.toContain('can see this')

    const ready = liveFixture('doc_a', ['name'])
    const mixed = pictureFixture({
      participants: {
        [BRANN.participant_id]: ready,
        [ANA.participant_id]: { ...ready, pending_delivery: true },
      },
    })
    const m = present({ status: 'live', state: mixed })
    expect(m.note).toBe('2 players can see this')
    expect(m.reveal).toMatchObject({ waiting: true })

    const both = liveFixture('doc_a', ['name'], { pending_delivery: true })
    const all = pictureFixture({ participants: { [BRANN.participant_id]: both, [ANA.participant_id]: { ...both } } })
    expect(present({ status: 'live', state: all }).note).toBe('Waiting to show 2 players')
  })
})

describe('a Stop that is not acknowledged (test 32, REVEAL-16, REVEAL-22)', () => {
  const state = pictureFixture({ table: liveFixture('doc_a', ['name']) })

  it('withdraws Open for the document while its Stop is unacknowledged, and keeps Stop', () => {
    const p = present({ status: 'live', state, stopping: new Set(['doc_a']) })
    expect(p.canOpen).toBe(false)
    expect(p.canStop).toBe(true)
    expect(p.reveal).toMatchObject({ state: 'revealed', stopFailed: false })
  })

  it('a Stop for all withdraws Open for every document', () => {
    expect(present({ status: 'live', state, stopping: new Set(['*']) }).canOpen).toBe(false)
  })

  it("says Couldn't stop showing once a Stop has failed", () => {
    const p = present({ status: 'live', state, stopping: new Set(['doc_a']), stopFailed: true })
    expect(p.reveal).toMatchObject({ state: 'revealed', stopFailed: true })
  })

  it('another document being stopped does not withdraw this one', () => {
    expect(present({ status: 'live', state, stopping: new Set(['doc_other']) }).canOpen).toBe(true)
  })
})

describe('revealProjections: what the workspace indicator lists (PR-2, REVEAL-14)', () => {
  const titles = new Map([['doc_a', 'Ondrey'], ['doc_b', 'Mira']])

  it('is empty for no picture and for a picture with nothing live', () => {
    expect(revealProjections(null, titles, seats)).toEqual([])
    expect(revealProjections(pictureFixture({}), titles, seats)).toEqual([])
  })

  it('lists each live document once, in slot order, with its title and who sees it', () => {
    const picture = pictureFixture({
      table: liveFixture('doc_a', ['name']),
      participants: { [BRANN.participant_id]: liveFixture('doc_b', ['name']), [ANA.participant_id]: null },
    })
    expect(revealProjections(picture, titles, seats)).toEqual([
      { documentId: 'doc_a', title: 'Ondrey', who: 'table', staleText: false, waiting: false },
      { documentId: 'doc_b', title: 'Mira', who: 'Brann', staleText: false, waiting: false },
    ])
  })

  it('a document live to two seats is one entry that names both, and past two it counts them', () => {
    const copy = liveFixture('doc_a', ['name'])
    const two = pictureFixture({ participants: { [BRANN.participant_id]: copy, [ANA.participant_id]: copy } })
    expect(revealProjections(two, titles, seats).map((entry) => [entry.documentId, entry.who])).toEqual([['doc_a', '2 players']])
  })

  it('a title that is not known yet is a document, never an id', () => {
    const picture = pictureFixture({ table: liveFixture('doc_zzz', ['name']) })
    expect(revealProjections(picture, titles, seats)[0].title).toBe('a document')
  })

  it('carries a stale copy and a held one', () => {
    const picture = pictureFixture({
      table: liveFixture('doc_a', ['name'], { stale_text: true }),
      participants: { [BRANN.participant_id]: liveFixture('doc_b', ['name'], { pending_delivery: true }) },
    })
    const [first, second] = revealProjections(picture, titles, seats)
    expect(first.staleText).toBe(true)
    expect(second.waiting).toBe(true)
  })

  it('an unknown seat is a player, and no seats yet is still a player', () => {
    const picture = pictureFixture({ participants: { par_gone000000000000000001: liveFixture('doc_a', ['name']) } })
    expect(revealProjections(picture, titles, seats)[0].who).toBe('a player')
    expect(revealProjections(picture, titles, [])[0].who).toBe('a player')
  })
})

describe('projectionSummary: Revealed · Ondrey (table) · +1 more', () => {
  const entry = (documentId: string, title: string, who: string) => ({ documentId, title, who, staleText: false, waiting: false })

  it('names the first and counts the rest', () => {
    expect(projectionSummary([entry('doc_a', 'Ondrey', 'table')])).toBe('Revealed · Ondrey (table)')
    expect(projectionSummary([entry('doc_a', 'Ondrey', 'table'), entry('doc_b', 'Mira', 'Brann')])).toBe('Revealed · Ondrey (table) · +1 more')
    expect(projectionSummary([entry('a', 'A', 'table'), entry('b', 'B', 'x'), entry('c', 'C', 'y')])).toBe('Revealed · A (table) · +2 more')
  })
})
