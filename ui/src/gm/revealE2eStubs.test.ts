/**
 * revealE2eStubs.test.ts -- the reveal e2e cannot pass on a shape the app would reject
 * (agent-forge-harness-1kg.7.3, brief section 9; the same promise `e2eStubs.test.ts` makes).
 *
 * `ui/e2e/revealStubs.ts` answers the reveal surface with a stateful world. Every body it
 * can answer, before and after a Confirm and a Stop, is parsed here with the real contract
 * schemas, and every id with the server's id shape.
 */

import { describe, expect, it } from 'vitest'
import { DOCUMENT, DOCUMENT_ID } from '../../e2e/workbenchStubs'
import {
  RevealWorld,
  SEAT_ALIAS,
  SEAT_ID,
  SEAT_PAGE,
  SESSION_ID,
  tableSessionAnswer,
} from '../../e2e/revealStubs'
import {
  RevealAnswerSchema,
  SeatPageSchema,
  TableSessionAnswerSchema,
  parseDocument,
  type RevealState,
} from './contracts'

/** The server mints a prefix and 22 to 60 base64url characters. */
const SERVER_ID = /^(ses|par|dis|doc|cmp)_[A-Za-z0-9_-]{22,60}$/

function stateOf(body: unknown): RevealState {
  const answer = RevealAnswerSchema.parse(body)
  if (answer.state === null) throw new Error('the stub answered no session')
  return answer.state
}

describe('the reveal e2e stubs are what the contract accepts', () => {
  it('every id is shaped like one the server mints', () => {
    for (const id of [SESSION_ID, SEAT_ID, DOCUMENT_ID]) expect(id).toMatch(SERVER_ID)
  })

  it('the table session parses as live, computed from the clock it is given, with the epoch the picture starts at', () => {
    const now = Date.parse('2026-10-01T12:00:00Z')
    const parsed = TableSessionAnswerSchema.parse(tableSessionAnswer(now))
    expect(parsed.session?.state).toBe('live')
    expect(parsed.session?.session_id).toBe(SESSION_ID)
    expect(Date.parse(parsed.session?.started_at ?? '')).toBe(now - 3_600_000)
    expect(Date.parse(parsed.session?.ends_at ?? '')).toBe(now + 6 * 3_600_000)
    expect(parsed.session?.reveal_epoch).toBe(3)
    expect(parsed.session?.audio_epoch).toBe(0)
    expect(parsed.session?.screens).toEqual([])
    // The session is live NOW, whenever the spec runs.
    const live = TableSessionAnswerSchema.parse(tableSessionAnswer())
    expect(Date.parse(live.session?.ends_at ?? '')).toBeGreaterThan(Date.now())
  })

  it('the seat page parses, with one confirmed seat named Brann', () => {
    const page = SeatPageSchema.parse(SEAT_PAGE)
    expect(page.items).toHaveLength(1)
    expect(page.items[0]).toMatchObject({ participant_id: SEAT_ID, alias: SEAT_ALIAS, status: 'confirmed' })
  })

  it('the seal answer is a document this client opens, at version 2, sealed', () => {
    const parsed = parseDocument(DOCUMENT)
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') {
      expect(parsed.value.version).toMatchObject({ number: 2, sealed: true })
      expect(parsed.value.archived).toBe(false)
    }
  })

  it('a world at rest answers a live session with the table slot present and empty, at epoch 3', () => {
    const state = stateOf(new RevealWorld().picture())
    expect(state.session_id).toBe(SESSION_ID)
    expect(state.reveal_epoch).toBe(3)
    expect(state.slots).toHaveLength(1)
    expect(state.slots[0]).toMatchObject({ slot: { kind: 'table' }, live: null })
  })

  it('a Confirm sets the table slot live with the mask as sent, and bumps the epoch and the sequence', () => {
    const world = new RevealWorld()
    const before = stateOf(world.picture())
    const after = stateOf(world.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name', 'qualifier', 'voice'] }))
    const slot = after.slots[0]
    expect(after.reveal_epoch).toBe(before.reveal_epoch + 1)
    expect(slot.seq).toBeGreaterThan(before.slots[0].seq)
    expect(slot.live).toMatchObject({
      document_id: DOCUMENT_ID,
      type: 'npc',
      version: 2,
      mask: ['name', 'qualifier', 'voice'],
      stale_text: false,
      pending_delivery: false,
    })
    expect(slot.live?.disclosure_id).toMatch(SERVER_ID)
  })

  it('a Stop clears the slot and advances the epoch again', () => {
    const world = new RevealWorld()
    const confirmed = stateOf(world.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name'] }))
    const stopped = stateOf(world.stop())
    expect(stopped.slots[0].live).toBeNull()
    expect(stopped.reveal_epoch).toBe(confirmed.reveal_epoch + 1)
    expect(stopped.slots[0].seq).toBeGreaterThan(confirmed.slots[0].seq)
  })

  it('two Confirms mint two disclosure ids, and the picture stays one the contract accepts', () => {
    const world = new RevealWorld()
    const first = stateOf(world.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name'] })).slots[0].live?.disclosure_id
    const second = stateOf(world.confirm({ document_id: DOCUMENT_ID, version: 2, mask: ['name', 'voice'] })).slots[0].live?.disclosure_id
    expect(first).not.toBe(second)
  })

  it('the document the stubs seal is the one the Workbench stubs open', () => {
    expect(DOCUMENT.document_id).toBe(DOCUMENT_ID)
  })
})
