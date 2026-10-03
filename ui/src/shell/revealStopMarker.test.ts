/**
 * revealStopMarker.test.ts -- the opaque pending-stop marker (agent-forge-harness-1kg.7.3 PR-2,
 * REVEAL-16). It holds ids and a number, nothing a GM typed; it is replayed only while the session
 * it names is still live and its epoch has not moved on, so it can never kill a later reveal.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FIXTURE_SESSION_ID, liveFixture, pictureFixture } from '../gm/revealFixtures'
import {
  clearPendingStop, pendingStopsKey, readPendingStops, replayable, writePendingStop, type PendingStop,
} from './revealStopMarker'

const STOP: PendingStop = {
  campaignId: 'cmp_A',
  documentId: 'doc_a',
  sessionId: FIXTURE_SESSION_ID,
  epoch: 3,
  commandId: 'cmd_pendingStopMarker0001',
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('storage', () => {
  it('round-trips a marker per account, and another account sees none', () => {
    writePendingStop('ada@example.com', STOP)
    expect(readPendingStops('ada@example.com')).toEqual([STOP])
    expect(readPendingStops('bo@example.com')).toEqual([])
  })

  it('holds opaque ids and one number only: no title, no text, no account', () => {
    writePendingStop('ada@example.com', STOP)
    const raw = localStorage.getItem(pendingStopsKey('ada@example.com')) ?? ''
    expect(Object.keys((JSON.parse(raw) as Array<Record<string, unknown>>)[0]).sort()).toEqual([
      'campaignId', 'commandId', 'documentId', 'epoch', 'sessionId',
    ])
  })

  it('a second press for the same document replaces its marker, and another document keeps its own', () => {
    writePendingStop('ada', STOP)
    writePendingStop('ada', { ...STOP, documentId: 'doc_b' })
    writePendingStop('ada', { ...STOP, epoch: 4 })
    expect(readPendingStops('ada').map((stop) => [stop.documentId, stop.epoch])).toEqual([['doc_b', 3], ['doc_a', 4]])
  })

  it('clears one marker and leaves the rest', () => {
    writePendingStop('ada', STOP)
    writePendingStop('ada', { ...STOP, documentId: '*' })
    clearPendingStop('ada', 'cmp_A', 'doc_a')
    expect(readPendingStops('ada').map((stop) => stop.documentId)).toEqual(['*'])
    clearPendingStop('ada', 'cmp_A', '*')
    expect(localStorage.getItem(pendingStopsKey('ada'))).toBeNull()
  })

  it.each([
    ['not json', 'nope'],
    ['not a list', '{"a":1}'],
    ['a bad id', JSON.stringify([{ ...STOP, documentId: 'doc a/../b' }])],
    ['a negative epoch', JSON.stringify([{ ...STOP, epoch: -1 }])],
    ['an extra field', JSON.stringify([{ ...STOP, title: 'Ondrey' }])],
  ])('ignores a stored value that is %s', (_name, raw) => {
    localStorage.setItem(pendingStopsKey('ada'), raw)
    expect(readPendingStops('ada')).toEqual([])
  })

  it('never throws when storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readPendingStops('ada')).toEqual([])
    expect(() => writePendingStop('ada', STOP)).not.toThrow()
    expect(() => clearPendingStop('ada', 'cmp_A', 'doc_a')).not.toThrow()
  })
})

describe('replayable: dropped when the session ended or the epoch moved on', () => {
  const live = (epoch: number, session?: string) =>
    pictureFixture({ epoch, ...(session === undefined ? {} : { sessionId: session }), table: liveFixture('doc_a', ['name']) })

  it('replays into the same session at the same epoch', () => {
    expect(replayable(STOP, live(3))).toBe(true)
  })

  it('drops it when the epoch moved on (a later reveal, or the Stop itself landed)', () => {
    expect(replayable(STOP, live(4))).toBe(false)
  })

  it('drops it when the session ended or another began', () => {
    expect(replayable(STOP, null)).toBe(false)
    expect(replayable(STOP, live(3, 'ses_anotherSession0000000001'))).toBe(false)
  })
})
