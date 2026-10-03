/**
 * useTavernSeats -- the tavern's read of the caller's own seats
 * (agent-forge-harness-30c, PR-2).
 *
 * One read per visit, every page up to `TAVERN_MAX_PAGES` (the order is the
 * client's, as for campaigns), through the campaign context's `readSeats`, which
 * makes no request for a signed-out session and drops an answer that outlives
 * its account. The hook keeps the pages in component state only: nothing about a
 * seat reaches web storage, the URL or the page title.
 *
 * - Started once on mount. StrictMode's second effect run starts nothing, so a
 *   visit reads once; `retry()` starts again from the first page, and a read it
 *   superseded is ignored whatever it answers.
 * - A page that fails ends the read: the pages already read stay shown
 *   (STATE-1) and `failed` is true until a retry succeeds.
 * - A cursor still left after the ceiling is not followed: the read is complete
 *   at `TAVERN_MAX_PAGES * 50` seats (ID-21).
 */
import * as React from 'react'
import type { PlayerSeat } from '../gm/contracts'
import type { SeatReadOutcome } from './campaignContext'
import { TAVERN_MAX_PAGES } from './tavernOrder'

export interface SeatsRead {
  /** A read has begun this visit (the status node is armed by it). */
  readonly started: boolean
  readonly reading: boolean
  readonly failed: boolean
  /** The read ended well: every page read, or the ceiling reached. */
  readonly complete: boolean
  readonly items: readonly PlayerSeat[]
  retry(): void
}

type Phase = 'idle' | 'reading' | 'done' | 'failed'

interface State {
  readonly phase: Phase
  readonly items: readonly PlayerSeat[]
}

function merged(known: readonly PlayerSeat[], page: readonly PlayerSeat[]): PlayerSeat[] {
  const seen = new Set(known.map((seat) => seat.campaign_id))
  return [...known, ...page.filter((seat) => !seen.has(seat.campaign_id))]
}

export function useTavernSeats(readSeats: (cursor: string | null) => Promise<SeatReadOutcome>): SeatsRead {
  const [state, setState] = React.useState<State>({ phase: 'idle', items: [] })
  const generation = React.useRef(0)
  const begun = React.useRef(false)

  const start = React.useCallback((): void => {
    generation.current += 1
    const mine = generation.current
    setState((prev) => ({ phase: 'reading', items: prev.items }))
    void (async () => {
      let items: readonly PlayerSeat[] = []
      let cursor: string | null = null
      for (let page = 0; page < TAVERN_MAX_PAGES; page += 1) {
        const outcome = await readSeats(cursor)
        if (mine !== generation.current) return
        if (outcome.kind !== 'ok') {
          // What this attempt read, else what was already on screen (STATE-1).
          setState((prev) => ({ phase: 'failed', items: items.length > 0 ? items : prev.items }))
          return
        }
        items = merged(items, outcome.items)
        cursor = outcome.nextCursor
        if (cursor === null) break
        setState({ phase: 'reading', items })
      }
      setState({ phase: 'done', items })
    })()
  }, [readSeats])

  React.useEffect(() => {
    if (begun.current) return
    begun.current = true
    start()
  }, [start])

  return {
    started: state.phase !== 'idle',
    reading: state.phase === 'reading',
    failed: state.phase === 'failed',
    complete: state.phase === 'done',
    items: state.items,
    retry: start,
  }
}
