/**
 * useTavernSeats.test.tsx -- the tavern's seat read (agent-forge-harness-30c,
 * PR-2, H-1 to H-6), with `readSeats` replaced by a recorder that can hold an
 * answer back, so each race really interleaves.
 */

import * as React from 'react'
import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { PlayerSeat } from '../gm/contracts'
import type { SeatReadOutcome } from './campaignContext'
import { TAVERN_MAX_PAGES } from './tavernOrder'
import { useTavernSeats } from './useTavernSeats'

function seat(id: string): PlayerSeat {
  return {
    schema_version: 1, campaign_id: id, campaign_name: `Table of ${id}`, alias: 'Brannoc',
    accepted_at: '2026-09-01T12:00:00Z', confirmed: true, tone: null, game_system: 'dnd5e',
    avatar_icon: 'castle', avatar_tone: 'gold', concluded: false, last_played_at: null, live: false,
  }
}
const ok = (ids: string[], next: string | null = null): SeatReadOutcome => ({
  kind: 'ok', items: ids.map(seat), nextCursor: next,
})
const FAILED: SeatReadOutcome = { kind: 'failed' }

interface Pending { cursor: string | null; answer: (outcome: SeatReadOutcome) => void }

/** A `readSeats` that answers from `script` (by cursor) or, with `hold`, waits to be told. */
function reader(script: (cursor: string | null, nth: number) => SeatReadOutcome | 'hold') {
  const cursors: Array<string | null> = []
  const pending: Pending[] = []
  const readSeats = (cursor: string | null): Promise<SeatReadOutcome> => new Promise((resolve) => {
    cursors.push(cursor)
    const answer = script(cursor, cursors.length)
    if (answer === 'hold') pending.push({ cursor, answer: resolve })
    else resolve(answer)
  })
  return { readSeats, cursors, pending }
}

const ids = (items: readonly PlayerSeat[]): string[] => items.map((s) => s.campaign_id)

describe('useTavernSeats (30c PR-2, H-1 to H-6)', () => {
  it('H-1 reads on mount, follows the cursor and ends complete with every page, a repeated seat shown once', async () => {
    const r = reader((cursor) => (cursor === null ? ok(['cmp_A', 'cmp_B'], 'p2') : ok(['cmp_B', 'cmp_C'])))
    const { result } = renderHook(() => useTavernSeats(r.readSeats))
    await act(async () => {})
    expect(r.cursors).toEqual([null, 'p2'])
    expect(result.current).toMatchObject({ started: true, reading: false, failed: false, complete: true })
    expect(ids(result.current.items)).toEqual(['cmp_A', 'cmp_B', 'cmp_C'])
  })

  it('H-2 is idle before its effect, and "reading" with the first page shown while the next is in flight', async () => {
    const r = reader((cursor) => (cursor === null ? ok(['cmp_A'], 'p2') : 'hold'))
    const { result } = renderHook(() => useTavernSeats(r.readSeats))
    await act(async () => {})
    expect(result.current).toMatchObject({ started: true, reading: true, complete: false, failed: false })
    expect(ids(result.current.items)).toEqual(['cmp_A'])
    await act(async () => r.pending[0].answer(ok(['cmp_B'])))
    expect(result.current).toMatchObject({ reading: false, complete: true })
    expect(ids(result.current.items)).toEqual(['cmp_A', 'cmp_B'])
  })

  it('H-3 stops at exactly TAVERN_MAX_PAGES reads however long the cursor goes on, and is complete there', async () => {
    const r = reader((_cursor, nth) => ok([`cmp_${nth}`], `p${nth + 1}`))
    const { result } = renderHook(() => useTavernSeats(r.readSeats))
    await act(async () => {})
    expect(r.cursors).toHaveLength(TAVERN_MAX_PAGES)
    expect(result.current.complete).toBe(true)
    expect(result.current.items).toHaveLength(TAVERN_MAX_PAGES)
  })

  it('H-4 a failed page ends the read as failed and keeps the pages already read', async () => {
    const r = reader((cursor) => (cursor === null ? ok(['cmp_A'], 'p2') : FAILED))
    const { result } = renderHook(() => useTavernSeats(r.readSeats))
    await act(async () => {})
    expect(result.current).toMatchObject({ failed: true, reading: false, complete: false })
    expect(ids(result.current.items)).toEqual(['cmp_A'])
    expect(r.cursors).toEqual([null, 'p2'])
  })

  it('H-5 StrictMode starts one read, not two; retry starts another from the first page and a read it superseded is ignored', async () => {
    const r = reader((_cursor, nth) => (nth === 1 ? 'hold' : nth === 2 ? FAILED : ok(['cmp_Z'])))
    const { result } = renderHook(() => useTavernSeats(r.readSeats), {
      wrapper: ({ children }: { children: React.ReactNode }) => <React.StrictMode>{children}</React.StrictMode>,
    })
    await act(async () => {})
    expect(r.cursors).toEqual([null])
    // A retry while the first read is still out supersedes it ...
    await act(async () => result.current.retry())
    expect(result.current.failed).toBe(true)
    // ... so the first read's late answer changes nothing.
    await act(async () => r.pending[0].answer(ok(['cmp_stale'])))
    expect(result.current.failed).toBe(true)
    expect(result.current.items).toEqual([])
    await act(async () => result.current.retry())
    expect(r.cursors).toEqual([null, null, null])
    expect(result.current).toMatchObject({ failed: false, complete: true })
    expect(ids(result.current.items)).toEqual(['cmp_Z'])
  })

  it('H-6 a failed retry keeps what was on screen when it read nothing new (STATE-1)', async () => {
    const r = reader((_cursor, nth) => (nth === 1 ? ok(['cmp_A']) : FAILED))
    const { result } = renderHook(() => useTavernSeats(r.readSeats))
    await act(async () => {})
    expect(ids(result.current.items)).toEqual(['cmp_A'])
    await act(async () => result.current.retry())
    expect(result.current.failed).toBe(true)
    expect(ids(result.current.items)).toEqual(['cmp_A'])
  })
})
