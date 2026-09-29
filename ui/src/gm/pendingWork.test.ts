/**
 * The pending-work model (1kg.3.5, I-12): association, retry, cancel, lost
 * runs, the server's cap and the stored-versus-live draw. Each test names the
 * mutation it exists to kill (brief section 11, critic items 15 to 20).
 *
 * Every invocation, request and error here goes through the contract's own
 * schema (`laneFixtures`, `threadFixtures`), so no case rests on a shape the
 * server would never send.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { ErrorInfoSchema, ToolInvocationRequestSchema } from './contracts'
import { turnsFromTimeline } from './gmTimeline'
import type { GmTurn } from './gmTimeline'
import { LANE_COPY, normaliseLane } from './laneState'
import type { LaneActionId, LaneView } from './laneState'
import { cardResult, documentResult, errorInfo, toolInvocation } from './laneFixtures'
import {
  CAP_RECHECK_MS,
  LOST_AFTER_MS,
  composerGate,
  emptyPendingWork,
  intentFor,
  overdue,
  reducePendingWork,
  requestFromEntry,
  turnsWithPendingWork,
} from './pendingWork'
import type { PendingEvent, PendingWork } from './pendingWork'
import { LIVE_CAMPAIGN, LIVE_CONVERSATION, chatEntry, toolEntry, toolRequest } from './threadFixtures'

const A = 'inv_11ve000000000001'
const B = 'inv_11ve000000000002'
const C = 'inv_11ve000000000003'
const OTHER_CONVERSATION = 'cnv_0ther000000001'
const T0 = Date.parse('2026-09-16T19:31:00Z')

const RETRYABLE = { code: 'backend_unavailable', retryable: true }
const FINAL = { code: 'validation_failed', message: 'That request could not be read.', retryable: false }
const BRIEF_TOO_LONG = { code: 'brief_too_long', message: 'That brief is too long.', retryable: false, field: 'brief' }
const THROTTLED_USER = { code: 'throttled_user', message: 'Slow down.', retryable: true, retry_after_s: 30 }
const DAILY = { code: 'throttled_daily', message: 'The daily limit is spent.', retryable: false }
const EXPIRED = { code: 'attempt_expired', message: 'The run stopped making progress.', retryable: true }

const DONE = { status: 'done', result: documentResult(), updated_at: '2026-09-16T19:31:40Z' }
const CANCELLED = { status: 'cancelled', cancel_requested: true, updated_at: '2026-09-16T19:31:40Z' }

function failed(error: Record<string, unknown>, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { status: 'failed', error: errorInfo(error), updated_at: '2026-09-16T19:31:40Z', ...overrides }
}

function start(id = A, overrides: Record<string, unknown> = {}, at = T0): PendingEvent {
  return { type: 'submitted', request: toolRequest({ invocation_id: id, ...overrides }), at }
}

function answer(id = A, overrides: Record<string, unknown> = {}): PendingEvent {
  return { type: 'answered', invocation: toolInvocation({ invocation_id: id, ...overrides }) }
}

function refuse(id: string, error: Record<string, unknown>, at = T0): PendingEvent {
  return { type: 'refused', invocationId: id, error: ErrorInfoSchema.parse(errorInfo(error)), at }
}

function then(state: PendingWork, ...events: PendingEvent[]): PendingWork {
  return events.reduce(reducePendingWork, state)
}

function run(...events: PendingEvent[]): PendingWork {
  return then(emptyPendingWork(), ...events)
}

/** The live turn `GmThread` would draw for a held run. */
function liveTurn(state: PendingWork, id = A, conversationId = LIVE_CONVERSATION) {
  const found = turnsWithPendingWork([], state, conversationId).liveTools.find((live) => live.turn.key === `live:${id}`)
  if (found === undefined) throw new Error(`no live turn for ${id}`)
  return found.turn
}

/** That turn's lane, exactly as `AssistantLane` builds it for a live turn. */
function lane(state: PendingWork, id = A): LaneView {
  const turn = liveTurn(state, id)
  return normaliseLane(turn.invocation, { lost: turn.live?.lost === true })
}

function statusOf(view: LaneView): string | null {
  return 'status' in view ? view.status : view.state === 'error' ? view.message : null
}

describe('pendingWork — an answer belongs to its own invocation', () => {
  it('an answer lands only on its own invocation, and a conversation lists only its own runs', () => {
    const state = run(start(A), start(B, { conversation_id: OTHER_CONVERSATION }), answer(A, DONE))

    expect(state.entries.get(A)?.invocation?.status).toBe('done')
    expect(state.entries.get(B)?.invocation).toBeNull()
    // MC-1: an answer for a run the model never started creates nothing.
    expect(then(state, answer(C, DONE))).toBe(state)
    expect(then(state, answer(C))).toBe(state)
    // MC-2: each conversation draws its own runs and no other.
    const keys = (id: string) => turnsWithPendingWork([], state, id).liveTools.map((live) => live.turn.key)
    expect(keys(LIVE_CONVERSATION)).toEqual([`live:${A}`])
    expect(keys(OTHER_CONVERSATION)).toEqual([`live:${B}`])
  })

  it('ignores an answer that names another tool than the run it claims to be', () => {
    const state = run(start(A))
    expect(then(state, answer(A, { tool_id: 'monster' }))).toBe(state)
  })

  it('results land beneath their own turns whatever the finish order', () => {
    const first = documentResult({ prose: 'A stranger who owes the ferryman.' })
    const second = documentResult({ prose: 'A ferryman who remembers every debt.' })
    const state = run(
      start(A, { brief: 'the hooded stranger' }),
      start(B, { brief: 'the ferryman' }),
      // The second finishes first.
      answer(B, { status: 'done', result: second, updated_at: '2026-09-16T19:31:30Z' }),
      answer(A, { status: 'done', result: first, updated_at: '2026-09-16T19:31:50Z' }),
    )
    const drawn = turnsWithPendingWork([], state, LIVE_CONVERSATION).liveTools.map(({ turn }) => ({
      key: turn.key,
      brief: turn.brief,
      id: turn.invocation.invocation_id,
      prose: turn.invocation.result?.prose,
    }))
    // MC-3: keyed by the invocation, in submission order, each result under its own brief.
    expect(drawn).toEqual([
      { key: `live:${A}`, brief: 'the hooded stranger', id: A, prose: 'A stranger who owes the ferryman.' },
      { key: `live:${B}`, brief: 'the ferryman', id: B, prose: 'A ferryman who remembers every debt.' },
    ])
  })
})

describe('pendingWork — retry reuses idempotency (RAIL-18)', () => {
  it('Try again re-sends the identical request, byte for byte', () => {
    // Surrounding spaces make an edited (trimmed) body visible.
    const sent = toolRequest({ brief: '  the hooded stranger  ', source_entry_id: 'ent_5ource01' })
    const state = then(emptyPendingWork(), { type: 'submitted', request: sent, at: T0 }, answer(A, failed(RETRYABLE)))

    const intent = intentFor(state, A, 'try-again')
    // MC-4 (a new id) and MC-5 (an edited body): the held request, unchanged.
    expect(intent).toEqual({ type: 'start', request: sent })
    if (intent?.type !== 'start') throw new Error('expected a start')
    expect(intent.request).toBe(state.entries.get(A)?.request)
    expect(JSON.stringify(intent.request)).toBe(JSON.stringify(sent))
    expect(intent.request.invocation_id).toBe(A)
  })

  it('a Try again sent again keeps the request it holds, not a body that differs', () => {
    const state = run(start(A), answer(A, failed(RETRYABLE)), start(A, { brief: 'something else entirely' }, T0 + 5_000))
    expect(state.entries.get(A)?.request).toEqual(toolRequest())
    expect(state.entries.get(A)?.submittedAt).toBe(T0 + 5_000)
    expect(state.order).toEqual([A])
  })

  it('a reloaded failure retries under its stored id, in the conversation’s own campaign', () => {
    const item = toolEntry({ source_entry_id: 'ent_5ource01' }, failed(RETRYABLE, { updated_at: '2026-09-16T19:35:30Z' }))
    if (item.kind !== 'ok' || item.value.entry_kind !== 'tool') throw new Error('expected a stored tool entry')
    const entry = item.value

    const request = requestFromEntry(entry, { conversationCampaignId: LIVE_CAMPAIGN, conversationId: LIVE_CONVERSATION })
    // MC-7: the stored id, never a fresh one; the brief and source as stored.
    expect(request).toEqual({
      schema_version: 1,
      invocation_id: 'inv_0a1b2c3d4e5f6a7b',
      tool_id: 'monster',
      brief: 'CR 5, drowned',
      campaign_id: LIVE_CAMPAIGN,
      conversation_id: LIVE_CONVERSATION,
      source_entry_id: 'ent_5ource01',
    })
    expect(ToolInvocationRequestSchema.safeParse(request).success).toBe(true)

    const bare = toolEntry()
    if (bare.kind !== 'ok' || bare.value.entry_kind !== 'tool') throw new Error('expected a stored tool entry')
    expect(requestFromEntry(bare.value, { conversationCampaignId: LIVE_CAMPAIGN, conversationId: LIVE_CONVERSATION })).not.toHaveProperty('source_entry_id')

    // Held with its stored answer, Try again sends that same request again.
    const state = run({ type: 'submitted', request, at: T0 }, { type: 'answered', invocation: entry.invocation })
    expect(intentFor(state, 'inv_0a1b2c3d4e5f6a7b', 'try-again')).toEqual({ type: 'start', request })
  })

  it('Check again reads status and never starts', () => {
    const state = run(start(A), answer(A), { type: 'lost', invocationId: A })
    // MC-8.
    expect(intentFor(state, A, 'check-again')).toEqual({ type: 'status', invocationId: A })
  })
})

// ── Every lane action from every state (critic 17) ───────────────────────────

const ACTIONS: readonly LaneActionId[] = ['cancel', 'try-again', 'edit-brief', 'check-again', 'run-again']

const STATES: Readonly<Record<string, PendingWork>> = {
  'sent, no answer yet': run(start()),
  working: run(start(), answer()),
  cancelling: run(start(), answer(), { type: 'cancel-requested', invocationId: A }),
  'checking (lost)': run(start(), answer(), { type: 'lost', invocationId: A }),
  'lost before any answer': run(start(), { type: 'lost', invocationId: A }),
  done: run(start(), answer(A, DONE)),
  'done after a cancel': run(start(), answer(A, { ...DONE, cancel_requested: true })),
  cancelled: run(start(), answer(A, CANCELLED)),
  'failed, retryable': run(start(), answer(A, failed(RETRYABLE))),
  'failed, final': run(start(), answer(A, failed(FINAL))),
  'throttled per user': run(start(), answer(A, failed(THROTTLED_USER))),
  'throttled daily': run(start(), answer(A, failed(DAILY))),
  'attempt expired': run(start(), answer(A, failed(EXPIRED))),
  'refused, final': run(start(), refuse(A, BRIEF_TOO_LONG)),
  'refused, retryable': run(start(), refuse(A, RETRYABLE)),
  'refused, daily': run(start(), refuse(A, DAILY)),
}

const OFFERED: Readonly<Record<string, readonly LaneActionId[]>> = {
  'sent, no answer yet': ['cancel'],
  working: ['cancel'],
  cancelling: ['cancel'],
  'checking (lost)': ['cancel', 'check-again'],
  'lost before any answer': ['cancel', 'check-again'],
  done: [],
  'done after a cancel': [],
  cancelled: ['run-again'],
  'failed, retryable': ['try-again'],
  'failed, final': ['edit-brief'],
  'throttled per user': ['try-again'],
  'throttled daily': [],
  'attempt expired': ['try-again'],
  'refused, final': ['edit-brief'],
  'refused, retryable': ['try-again'],
  'refused, daily': [],
}

describe('pendingWork — a lane action is answered only where the lane offers it', () => {
  it('offers exactly the actions the drawn lane offers, from every state', () => {
    const offered = Object.fromEntries(
      Object.entries(STATES).map(([name, state]) => [name, ACTIONS.filter((action) => intentFor(state, A, action) !== null)]),
    )
    // MC-6 (Try again from done or cancelled), MC-25 (Cancel from done).
    expect(offered).toEqual(OFFERED)
    for (const [name, state] of Object.entries(STATES)) {
      const view = lane(state)
      const shown = view.state === 'done' || view.state === 'unreadable' ? [] : view.actions
      expect([...shown].sort(), name).toEqual([...OFFERED[name]].sort())
    }
  })

  it('turns each offered action into its intent, and only Try again into a start', () => {
    const held = toolRequest()
    const expected: Record<LaneActionId, unknown> = {
      cancel: { type: 'cancel', invocationId: A },
      'try-again': { type: 'start', request: held },
      'edit-brief': { type: 'arm', toolId: 'npc', brief: held.brief },
      'check-again': { type: 'status', invocationId: A },
      'run-again': { type: 'arm', toolId: 'npc', brief: held.brief },
    }
    let checked = 0
    for (const [name, state] of Object.entries(STATES)) {
      for (const action of OFFERED[name]) {
        expect(intentFor(state, A, action), `${name} / ${action}`).toEqual(expected[action])
        checked += 1
      }
      for (const action of ACTIONS) {
        if (action !== 'try-again') expect(intentFor(state, A, action)?.type).not.toBe('start')
      }
    }
    expect(checked).toBe(14)
  })

  it('answers nothing for a run it does not hold', () => {
    for (const action of ACTIONS) expect(intentFor(STATES.working, C, action)).toBeNull()
  })
})

describe('pendingWork — cancellation (RAIL-23)', () => {
  it('cancel keeps its place until a terminal state, and a late finish keeps its result', () => {
    const asked = run(start(), answer(A, { updated_at: '2026-09-16T19:31:10Z' }), { type: 'cancel-requested', invocationId: A })
    expect(lane(asked)).toMatchObject({ state: 'working', status: LANE_COPY.cancelling, cancelling: true })

    // MC-9: a working answer that crossed the cancel does not undo it.
    const crossed = then(asked, answer(A, { updated_at: '2026-09-16T19:31:20Z' }))
    expect(crossed.entries.get(A)?.invocation?.updated_at).toBe('2026-09-16T19:31:20Z')
    expect(lane(crossed)).toMatchObject({ state: 'working', status: LANE_COPY.cancelling })

    // The provider had already finished: the result is kept, and says so.
    const late = then(crossed, answer(A, { ...DONE, cancel_requested: true }))
    expect(lane(late)).toMatchObject({ state: 'done', note: LANE_COPY.lateFinish })
    expect(liveTurn(late).invocation.result?.prose).toBe(documentResult().prose)
    expect(late.entries.get(A)?.cancelling).toBe(false)
  })

  it('a cancelled run is terminal: a later working answer of that attempt is ignored', () => {
    const cancelled = run(start(), answer(A), { type: 'cancel-requested', invocationId: A }, answer(A, CANCELLED))
    expect(lane(cancelled)).toMatchObject({ state: 'cancelled', status: LANE_COPY.cancelled })
    // MC-10.
    expect(then(cancelled, answer(A, { updated_at: '2026-09-16T19:32:00Z' }))).toBe(cancelled)
  })

  it('never un-sets cancel_requested within an attempt', () => {
    const flagged = run(start(), answer(A, { cancel_requested: true, updated_at: '2026-09-16T19:31:20Z' }))
    expect(then(flagged, answer(A, { cancel_requested: false, updated_at: '2026-09-16T19:31:30Z' }))).toBe(flagged)
    // The server flags a finished run when the cancel lands after it (RAIL-23).
    const done = run(start(), answer(A, DONE))
    const flaggedLate = then(done, answer(A, { ...DONE, cancel_requested: true, updated_at: '2026-09-16T19:31:45Z' }))
    expect(lane(flaggedLate)).toMatchObject({ state: 'done', note: LANE_COPY.lateFinish })
  })

  it('a failed answer also ends a cancel', () => {
    const state = run(start(), answer(A), { type: 'cancel-requested', invocationId: A }, answer(A, failed(RETRYABLE)))
    expect(state.entries.get(A)?.cancelling).toBe(false)
    expect(lane(state).state).toBe('error')
  })

  it('asks nothing more of a run that has ended, or one it does not hold', () => {
    const done = run(start(), answer(A, DONE))
    expect(then(done, { type: 'cancel-requested', invocationId: A })).toBe(done)
    expect(then(done, { type: 'cancel-requested', invocationId: C })).toBe(done)
    const asked = then(STATES.working, { type: 'cancel-requested', invocationId: A })
    expect(then(asked, { type: 'cancel-requested', invocationId: A })).toBe(asked)
  })
})

describe('pendingWork — staleness', () => {
  it('a stale answer never overwrites a fresher one', () => {
    const second = run(start(), answer(A, failed(RETRYABLE)), answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z' }))
    expect(second.entries.get(A)?.invocation).toMatchObject({ status: 'working', attempt: 2 })
    // MC-11: a lower attempt, or an earlier update of the same attempt, is ignored.
    expect(then(second, answer(A, failed(RETRYABLE, { updated_at: '2026-09-16T19:33:00Z' })))).toBe(second)
    expect(then(second, answer(A, { attempt: 2, updated_at: '2026-09-16T19:31:59Z' }))).toBe(second)
  })

  it('compares updated_at as an instant, never as a string', () => {
    // As strings "…24.5Z" sorts before "…24Z"; as instants it is half a second later.
    const whole = run(start(), answer(A, { updated_at: '2026-09-16T19:31:24Z' }))
    const later = then(whole, answer(A, { cancel_requested: true, updated_at: '2026-09-16T19:31:24.5Z' }))
    // MC-21, both ways.
    expect(later.entries.get(A)?.invocation?.updated_at).toBe('2026-09-16T19:31:24.5Z')
    const half = run(start(), answer(A, { updated_at: '2026-09-16T19:31:24.5Z' }))
    expect(then(half, answer(A, { cancel_requested: true, updated_at: '2026-09-16T19:31:24Z' }))).toBe(half)
  })

  it('a terminal state is final for its attempt', () => {
    const done = run(start(), answer(A, DONE))
    // MC-22: a same-attempt failure after done.
    expect(then(done, answer(A, failed(RETRYABLE, { updated_at: '2026-09-16T19:32:00Z' })))).toBe(done)
    const failure = run(start(), answer(A, failed(FINAL)))
    expect(then(failure, answer(A, { updated_at: '2026-09-16T19:32:00Z' }))).toBe(failure)
  })

  it('a higher attempt follows only a working or failed one', () => {
    const nextAttempt = answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z' })
    // MC-23: never after done or cancelled, where the server starts nothing.
    expect(then(STATES.done, nextAttempt)).toBe(STATES.done)
    expect(then(STATES.cancelled, nextAttempt)).toBe(STATES.cancelled)
    // After a failure it is the retry; after working, a retry of an expiry this client missed.
    expect(then(STATES['failed, retryable'], nextAttempt).entries.get(A)?.invocation?.attempt).toBe(2)
    expect(then(STATES.working, nextAttempt).entries.get(A)?.invocation?.attempt).toBe(2)
  })

  it('a new attempt is watched afresh: not lost, not cancelling', () => {
    const lost = run(start(), answer(A), { type: 'lost', invocationId: A })
    const retried = then(lost, answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z' }))
    expect(retried.entries.get(A)).toMatchObject({ lost: false, cancelling: false })
    expect(lane(retried).state).toBe('working')
  })
})

describe('pendingWork — a run waited on too long (RAIL-21)', () => {
  it('a lost run is checked on, never re-run', () => {
    const working = run(start(), answer())
    expect(overdue(working, T0 + LOST_AFTER_MS)).toEqual([])
    expect(overdue(working, T0 + LOST_AFTER_MS + 1)).toEqual([A])

    const lost = then(working, { type: 'lost', invocationId: A })
    expect(overdue(lost, T0 + LOST_AFTER_MS + 1)).toEqual([])
    // MC-12: Checking on…, with Check again and Cancel, and no Try again.
    expect(lane(lost)).toMatchObject({ state: 'checking', status: 'Checking on NPC…' })
    expect(intentFor(lost, A, 'try-again')).toBeNull()
    expect(then(lost, { type: 'lost', invocationId: A })).toBe(lost)
  })

  it('lists a run with no answer at all, and never one that has ended or was refused', () => {
    const later = T0 + LOST_AFTER_MS + 1
    expect(overdue(run(start()), later)).toEqual([A])
    for (const name of ['done', 'cancelled', 'failed, retryable', 'refused, retryable']) {
      expect(overdue(STATES[name], later), name).toEqual([])
      expect(then(STATES[name], { type: 'lost', invocationId: A }), name).toBe(STATES[name])
    }
    expect(then(STATES.working, { type: 'lost', invocationId: C })).toBe(STATES.working)
  })

  it('a Try again restarts the wait', () => {
    const retried = run(start(), answer(A, failed(RETRYABLE)), start(A, {}, T0 + 60_000))
    expect(retried.entries.get(A)?.submittedAt).toBe(T0 + 60_000)
  })
})

describe('pendingWork — the composer gate (RAIL-16, X-5)', () => {
  it('a working lane never makes Send unavailable', () => {
    const ids = ['inv_w0rking000000001', 'inv_w0rking000000002', 'inv_w0rking000000003', 'inv_w0rking000000004', 'inv_w0rking000000005']
    const busy = run(...ids.flatMap((id) => [start(id), answer(id)]))
    expect([...busy.entries.values()].filter((entry) => entry.invocation?.status === 'working')).toHaveLength(5)
    // MC-13: no count of working lanes sets the block.
    expect(composerGate(busy, { chatInFlight: false, now: T0 })).toEqual({ chatPending: false, workbenchBusy: false })
    expect(composerGate(busy, { chatInFlight: true, now: T0 })).toEqual({ chatPending: true, workbenchBusy: false })
  })

  it('the server’s cap blocks Send until it can have changed', () => {
    const refusedAt = T0 + 1_000
    const capped = run(start(A), answer(A), start(B), answer(B), start(C), refuse(C, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true, in_flight: [A, B] }, refusedAt))

    // A first submit that got nothing leaves no lane; its draft is the controller's.
    expect(capped.entries.has(C)).toBe(false)
    expect(capped.order).toEqual([A, B])
    expect(turnsWithPendingWork([], capped, LIVE_CONVERSATION).liveTools.map((live) => live.turn.key)).toEqual([`live:${A}`, `live:${B}`])
    expect(capped.capInFlight).toEqual([A, B])

    const gate = (state: PendingWork, now: number) => composerGate(state, { chatInFlight: false, now }).workbenchBusy
    // MC-15: not cleared at once; MC-14: cleared when it can have changed.
    expect(gate(capped, refusedAt)).toBe(true)
    expect(gate(capped, refusedAt + CAP_RECHECK_MS - 1)).toBe(true)
    expect(gate(capped, refusedAt + CAP_RECHECK_MS)).toBe(false)
    expect(gate(then(capped, answer(A, DONE)), refusedAt + 1)).toBe(false)
    // Still working frees nothing.
    expect(gate(then(capped, answer(A, { updated_at: '2026-09-16T19:31:30Z' })), refusedAt + 1)).toBe(true)
  })

  it('a stale terminal answer frees nothing', () => {
    const cap = refuse(C, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true, in_flight: [A, B] })
    // A is on its second attempt when the cap answers; its first attempt's failure arrives late.
    const capped = run(start(A), answer(A, failed(RETRYABLE)), start(A), answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z' }), start(B), answer(B), start(C), cap)
    const late = then(capped, answer(A, failed(RETRYABLE, { updated_at: '2026-09-16T19:33:00Z' })))
    expect(late).toBe(capped)
    expect(composerGate(late, { chatInFlight: false, now: T0 }).workbenchBusy).toBe(true)
  })

  it('a run the model never started clears the block only when the refusal named it and it has ended', () => {
    const other = 'inv_0therTab0000001'
    const capped = run(start(A), refuse(A, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true, in_flight: [other] }))
    const gate = (state: PendingWork) => composerGate(state, { chatInFlight: false, now: T0 }).workbenchBusy
    expect(gate(capped)).toBe(true)
    expect(then(capped, answer(other))).toBe(capped)
    expect(then(capped, answer(C, DONE))).toBe(capped)
    const freed = then(capped, answer(other, DONE))
    expect(gate(freed)).toBe(false)
    // Only the block: nothing is drawn for it.
    expect(freed.entries.size).toBe(0)
  })

  it('a cap with no runs named still lifts after its recheck', () => {
    const capped = run(start(A), refuse(A, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true }))
    expect(capped.capInFlight).toEqual([])
    expect(composerGate(capped, { chatInFlight: false, now: T0 + CAP_RECHECK_MS }).workbenchBusy).toBe(false)
  })

  it('a Try again refused by the cap keeps its failed lane and its invocation', () => {
    const failure = run(start(A), answer(A, failed(RETRYABLE)))
    const capped = then(failure, start(A, {}, T0 + 5_000), refuse(A, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true, in_flight: [B] }, T0 + 5_000))
    // MC-24.
    expect(capped.entries.get(A)?.invocation).toEqual(failure.entries.get(A)?.invocation)
    expect(lane(capped)).toMatchObject({ state: 'error', actions: ['try-again'] })
    expect(composerGate(capped, { chatInFlight: false, now: T0 + 5_000 }).workbenchBusy).toBe(true)
    // So does a refused start's lane, on its Try again.
    const refusedTwice = then(STATES['refused, retryable'], refuse(A, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true }))
    expect(refusedTwice.entries.get(A)?.refusal?.code).toBe('backend_unavailable')
  })
})

describe('pendingWork — refusals', () => {
  it('a refused start is an honest failed lane', () => {
    // MC-16: never dropped, never drawn working.
    expect(lane(STATES['refused, final'])).toMatchObject({ state: 'error', message: 'That brief is too long.', actions: ['edit-brief'] })
    expect(intentFor(STATES['refused, final'], A, 'edit-brief')).toEqual({ type: 'arm', toolId: 'npc', brief: toolRequest().brief })
    expect(lane(STATES['refused, retryable'])).toMatchObject({ state: 'error', actions: ['try-again'] })
    expect(intentFor(STATES['refused, retryable'], A, 'try-again')).toEqual({ type: 'start', request: toolRequest() })
    expect(lane(STATES['refused, daily'])).toMatchObject({ state: 'error', message: LANE_COPY.dailyCap, actions: [] })
  })

  it('a Try again refused before its attempt started shows the refusal, and an answer clears it', () => {
    const throttled = run(start(A), answer(A, failed(RETRYABLE)), start(A), refuse(A, THROTTLED_USER))
    expect(lane(throttled)).toMatchObject({ state: 'error', actions: ['try-again'] })
    expect(statusOf(lane(throttled))).toMatch(/^That's a lot at once/)
    const through = then(throttled, answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z' }))
    expect(through.entries.get(A)?.refusal).toBeNull()
    expect(lane(through).state).toBe('working')
  })

  it('a refusal is not the news of a run that is working or has finished, nor of one it does not hold', () => {
    expect(then(STATES.working, refuse(A, RETRYABLE))).toBe(STATES.working)
    expect(then(STATES.done, refuse(A, RETRYABLE))).toBe(STATES.done)
    expect(then(STATES.working, refuse(C, RETRYABLE))).toBe(STATES.working)
  })
})

describe('pendingWork — the thread it draws', () => {
  const STORED_ID = 'inv_0a1b2c3d4e5f6a7b'
  const stored = (invocation: Record<string, unknown> = {}): GmTurn[] =>
    turnsFromTimeline([
      chatEntry({ entry_id: 'ent_1' }),
      toolEntry({ entry_id: 'ent_7001' }, invocation),
      chatEntry({ entry_id: 'ent_2' }),
    ])
  const heldAs = (...events: PendingEvent[]) =>
    run({ type: 'submitted', request: toolRequest({ invocation_id: STORED_ID, tool_id: 'monster', brief: 'CR 5, drowned' }), at: T0 }, ...events)
  const monster = (overrides: Record<string, unknown>): PendingEvent => answer(STORED_ID, { tool_id: 'monster', ...overrides })

  it('a stored and a live copy of one run draw once, where it is stored, from the fresher copy', () => {
    const fresherLive = heldAs(monster({ status: 'done', result: cardResult(), updated_at: '2026-09-16T19:36:00Z' }))
    const { turns, liveTools } = turnsWithPendingWork(stored(), fresherLive, LIVE_CONVERSATION)
    // MC-17: once.
    expect(liveTools).toEqual([])
    expect(turns.map((turn) => turn.key)).toEqual(['entry:ent_1', 'entry:ent_7001', 'entry:ent_2'])
    const [, tool] = turns
    if (tool.kind !== 'tool') throw new Error('expected a tool turn')
    // MC-18: the fresher copy is drawn, both ways round.
    expect(tool).toMatchObject({ entryId: 'ent_7001', live: { lost: false } })
    expect(tool.invocation).toMatchObject({ status: 'done', updated_at: '2026-09-16T19:36:00Z' })

    const fresherStored = turnsWithPendingWork(
      stored({ status: 'done', result: cardResult(), updated_at: '2026-09-16T19:37:00Z' }),
      heldAs(monster({ updated_at: '2026-09-16T19:35:30Z' })),
      LIVE_CONVERSATION,
    ).turns[1]
    if (fresherStored.kind !== 'tool') throw new Error('expected a tool turn')
    expect(fresherStored.invocation).toMatchObject({ status: 'done', updated_at: '2026-09-16T19:37:00Z' })
  })

  it('leaves a stored turn it does not hold exactly as it was, and never draws another conversation’s run', () => {
    const hydrated = stored()
    const { turns } = turnsWithPendingWork(hydrated, run(start(A)), LIVE_CONVERSATION)
    turns.forEach((turn, index) => expect(turn).toBe(hydrated[index]))
    const elsewhere = run({ type: 'submitted', request: toolRequest({ invocation_id: STORED_ID, tool_id: 'monster', conversation_id: OTHER_CONVERSATION }), at: T0 })
    const drawn = turnsWithPendingWork(hydrated, elsewhere, LIVE_CONVERSATION)
    expect(drawn.turns[1]).toBe(hydrated[1])
    expect(drawn.liveTools).toEqual([])
  })

  it('draws a run with no answer yet as working, keyed by its id, with no entry and its submit time', () => {
    const { liveTools } = turnsWithPendingWork(stored(), run(start(A, {}, T0 + 42)), LIVE_CONVERSATION)
    expect(liveTools).toHaveLength(1)
    const [{ turn, submittedAt }] = liveTools
    expect(submittedAt).toBe(T0 + 42)
    expect(turn).toMatchObject({ kind: 'tool', key: `live:${A}`, entryId: null, brief: toolRequest().brief, live: { lost: false } })
    expect(turn.invocation).toMatchObject({ invocation_id: A, tool_id: 'npc', status: 'working', attempt: 1, cancel_requested: false })
    expect(lane(run(start(A)))).toMatchObject({ state: 'working', status: 'Writing the dossier…' })
  })

  it('draws a run whose submit time is not a date rather than throwing', () => {
    expect(liveTurn(run(start(A, {}, Number.NaN))).invocation.created_at).toBe('1970-01-01T00:00:00.000Z')
  })

  it('never stores the view it draws for a run with no answer', () => {
    const state = run(start(A), { type: 'cancel-requested', invocationId: A })
    liveTurn(state)
    expect(state.entries.get(A)?.invocation).toBeNull()
    expect(liveTurn(state).invocation.cancel_requested).toBe(true)
  })
})

describe('pendingWork — a Try again in flight is a run, not the failure it retries (RAIL-15, RAIL-21, RAIL-23)', () => {
  const RETRIED_AT = T0 + 60_000
  /** The real order: start, a retryable failure, then Try again — before the server answers it. */
  const retrying = () => run(start(), answer(A, failed(RETRYABLE)), start(A, {}, RETRIED_AT))
  const attemptTwo = (overrides: Record<string, unknown> = {}) => answer(A, { attempt: 2, updated_at: '2026-09-16T19:32:00Z', ...overrides })

  it('is RAIL-15’s working lane, as the attempt it starts, with Cancel and never a second Try again', () => {
    const state = retrying()
    expect(lane(state)).toMatchObject({ state: 'working', status: 'Writing the dossier…', cancelling: false, actions: ['cancel'] })
    // The attempt it starts, so `Still working…` times it afresh.
    expect(liveTurn(state).invocation).toMatchObject({ status: 'working', attempt: 2, cancel_requested: false, result: null, error: null })
    // A view only: the failure it retries is still what the model holds.
    expect(state.entries.get(A)).toMatchObject({ awaiting: true, invocation: { status: 'failed', attempt: 1 } })
    expect(intentFor(state, A, 'try-again')).toBeNull()
    expect(intentFor(state, A, 'cancel')).toEqual({ type: 'cancel', invocationId: A })
  })

  it('restarts the wait: overdue lists it 120 s after the Try again, and lost is checking', () => {
    const state = retrying()
    expect(overdue(state, RETRIED_AT + LOST_AFTER_MS)).toEqual([])
    expect(overdue(state, RETRIED_AT + LOST_AFTER_MS + 1)).toEqual([A])
    const lost = then(state, { type: 'lost', invocationId: A })
    expect(lost.entries.get(A)?.lost).toBe(true)
    expect(lane(lost)).toMatchObject({ state: 'checking', status: 'Checking on NPC…', actions: ['check-again', 'cancel'] })
    expect(intentFor(lost, A, 'check-again')).toEqual({ type: 'status', invocationId: A })
    expect(intentFor(lost, A, 'try-again')).toBeNull()
    // The attempt the Try again started is the one that was lost, so its working answer stays checked on (D-4).
    expect(lane(then(lost, attemptTwo()))).toMatchObject({ state: 'checking' })
  })

  it('can be cancelled, and the cancel holds across the attempt it started until that attempt ends', () => {
    const asked = then(retrying(), { type: 'cancel-requested', invocationId: A })
    expect(asked.entries.get(A)?.cancelling).toBe(true)
    expect(lane(asked)).toMatchObject({ state: 'working', status: LANE_COPY.cancelling, cancelling: true })
    const started = then(asked, attemptTwo())
    expect(lane(started)).toMatchObject({ state: 'working', status: LANE_COPY.cancelling, cancelling: true })
    const ended = then(started, attemptTwo({ ...CANCELLED, updated_at: '2026-09-16T19:32:10Z' }))
    expect(lane(ended)).toMatchObject({ state: 'cancelled', actions: ['run-again'] })
    expect(ended.entries.get(A)?.cancelling).toBe(false)
  })

  it('ends with an answer to it, and a stale answer does not end it', () => {
    const done = then(retrying(), attemptTwo(DONE))
    expect(done.entries.get(A)?.awaiting).toBe(false)
    expect(lane(done)).toMatchObject({ state: 'done' })
    expect(overdue(done, RETRIED_AT + LOST_AFTER_MS + 1)).toEqual([])
    expect(lane(then(retrying(), attemptTwo(failed(RETRYABLE))))).toMatchObject({ state: 'error', actions: ['try-again'] })
    // D-8: a status read that still shows the failure it retried means the Try again never started.
    expect(lane(then(retrying(), answer(A, failed(RETRYABLE))))).toMatchObject({ state: 'error', actions: ['try-again'] })
    // Older than the failure held: not an answer to anything.
    const state = retrying()
    expect(then(state, answer(A, failed(RETRYABLE, { updated_at: '2026-09-16T19:31:30Z' })))).toBe(state)
    // A refusal answers it too.
    const throttled = then(retrying(), refuse(A, THROTTLED_USER))
    expect(throttled.entries.get(A)?.awaiting).toBe(false)
    expect(lane(throttled)).toMatchObject({ state: 'error', actions: ['try-again'] })
    expect(statusOf(lane(throttled))).toMatch(/^That's a lot at once/)
  })

  it('a refused start’s Try again that the cap refuses keeps the lane it was sent from, in the real order', () => {
    const sent = run(start(), refuse(A, RETRYABLE), start(A, {}, RETRIED_AT))
    expect(lane(sent)).toMatchObject({ state: 'working', actions: ['cancel'] })
    const capped = then(sent, refuse(A, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true, in_flight: [B] }, RETRIED_AT + 1_000))
    // H-2: the entry, its brief and its refusal stay; only the cap block is new.
    expect(capped.order).toEqual([A])
    expect(capped.entries.get(A)).toMatchObject({ awaiting: false, refusal: { code: 'backend_unavailable' }, request: toolRequest() })
    expect(lane(capped)).toMatchObject({ state: 'error', actions: ['try-again'] })
    expect(intentFor(capped, A, 'try-again')).toEqual({ type: 'start', request: toolRequest() })
    expect(composerGate(capped, { chatInFlight: false, now: RETRIED_AT + 1_001 }).workbenchBusy).toBe(true)
  })

  it('a refusal answers a send over a working run and changes nothing else', () => {
    const resent = run(start(), answer(), start(A, {}, T0 + 5_000))
    expect(resent.entries.get(A)?.awaiting).toBe(true)
    const refusedAgain = then(resent, refuse(A, RETRYABLE))
    expect(refusedAgain.entries.get(A)).toMatchObject({ awaiting: false, refusal: null, invocation: { status: 'working', attempt: 1 } })
    expect(lane(refusedAgain)).toMatchObject({ state: 'working', actions: ['cancel'] })
  })
})

describe('pendingWork — a lane action answers the lane as drawn from a stored copy', () => {
  const STORED_ID = 'inv_0a1b2c3d4e5f6a7b'
  /** A stored `/monster` failure, a minute after the held run's last answer. */
  const storedItem = () => toolEntry({ entry_id: 'ent_7001' }, failed(RETRYABLE, { updated_at: '2026-09-16T19:36:00Z' }))
  const storedFailure = () => {
    const item = storedItem()
    if (item.kind !== 'ok' || item.value.entry_kind !== 'tool') throw new Error('expected a stored tool entry')
    return item.value
  }
  const drawnLane = (state: PendingWork) => {
    const [turn] = turnsWithPendingWork(turnsFromTimeline([storedItem()]), state, LIVE_CONVERSATION).turns
    if (turn.kind !== 'tool') throw new Error('expected a tool turn')
    return { invocation: turn.invocation, view: normaliseLane(turn.invocation, { lost: turn.live?.lost === true }) }
  }

  it('a reloaded failure’s Try again is drawn in flight where it is stored, never as that failure', () => {
    const entry = storedFailure()
    const request = requestFromEntry(entry, { conversationCampaignId: LIVE_CAMPAIGN, conversationId: LIVE_CONVERSATION })
    const state = run({ type: 'submitted', request, at: T0 })
    const { invocation, view } = drawnLane(state)
    expect(invocation).toMatchObject({ status: 'working', attempt: 2 })
    expect(view).toMatchObject({ state: 'working', status: 'Building the stat block…', actions: ['cancel'] })
    expect(intentFor(state, STORED_ID, 'cancel', entry.invocation)).toEqual({ type: 'cancel', invocationId: STORED_ID })
    expect(intentFor(state, STORED_ID, 'try-again', entry.invocation)).toBeNull()
    expect(overdue(state, T0 + LOST_AFTER_MS + 1)).toEqual([STORED_ID])
  })

  it('offers exactly what the drawn lane offers when the stored copy is the fresher', () => {
    const entry = storedFailure()
    const request = toolRequest({ invocation_id: STORED_ID, tool_id: 'monster', brief: 'CR 5, drowned' })
    const held = run({ type: 'submitted', request, at: T0 }, answer(STORED_ID, { tool_id: 'monster', updated_at: '2026-09-16T19:35:00Z' }))
    const { view } = drawnLane(held)
    expect(view).toMatchObject({ state: 'error', actions: ['try-again'] })
    // M-1: the lane as drawn, not the model's own copy.
    expect(ACTIONS.filter((action) => intentFor(held, STORED_ID, action, entry.invocation) !== null)).toEqual(['try-again'])
    expect(intentFor(held, STORED_ID, 'try-again', entry.invocation)).toEqual({ type: 'start', request })
    expect(intentFor(held, STORED_ID, 'cancel', entry.invocation)).toBeNull()
    // With no stored copy it answers for the run it holds, which is working.
    expect(intentFor(held, STORED_ID, 'cancel')).toEqual({ type: 'cancel', invocationId: STORED_ID })
  })
})

describe('pendingWork — X-7', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('the model keeps nothing outside memory', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const cookie = vi.spyOn(Document.prototype, 'cookie', 'set')
    const pushState = vi.spyOn(history, 'pushState')
    const replaceState = vi.spyOn(history, 'replaceState')
    const before = { local: JSON.stringify({ ...localStorage }), session: JSON.stringify({ ...sessionStorage }), url: location.href }

    const brief = 'the canary who knows the queen’s secret'
    const state = run(
      start(A, { brief }),
      answer(A),
      { type: 'cancel-requested', invocationId: A },
      { type: 'lost', invocationId: A },
      start(B, { brief }),
      refuse(B, FINAL),
      start(C, { brief }),
      refuse(C, { code: 'cap_reached', message: 'Two tools are already running.', retryable: true }),
    )
    turnsWithPendingWork(turnsFromTimeline([toolEntry()]), state, LIVE_CONVERSATION)
    for (const action of ACTIONS) intentFor(state, A, action)
    overdue(state, T0 + LOST_AFTER_MS * 2)
    composerGate(state, { chatInFlight: false, now: T0 })

    // MC-19.
    expect(setItem).not.toHaveBeenCalled()
    expect(cookie).not.toHaveBeenCalled()
    expect(pushState).not.toHaveBeenCalled()
    expect(replaceState).not.toHaveBeenCalled()
    expect(JSON.stringify({ ...localStorage })).toBe(before.local)
    expect(JSON.stringify({ ...sessionStorage })).toBe(before.session)
    expect(location.href).toBe(before.url)
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('canary')
  })
})
