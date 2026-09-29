/**
 * The pending-work model (1kg.3.5, I-12; the lead's Q-1 ruling of 2026-09-29):
 * what this client knows about the tool runs it started, as pure functions.
 *
 * It holds each run under its own `invocation_id` — the idempotency key the
 * client minted (RAIL-18) — and nothing else associates an answer with a turn:
 * not text, not a position, not arrival order. So results land beneath their
 * own turns in whatever order they finish (RAIL-16), a retry re-sends the held
 * request unchanged, and an answer for a run the model never started changes
 * no turn at all.
 *
 * **What it is not.** It sends nothing, reads no clock and mounts nothing.
 * `1kg.4.5` owns the transport (`1kg.4.1`'s three routes), the timers that
 * drive `overdue`, mounting `ToolComposer`, passing the lane's action props
 * and interleaving live tool turns with live chat turns; it feeds this model
 * the events below and renders what `turnsWithPendingWork` returns. Every
 * time is a parameter (`at`, `now`), so a test never waits.
 *
 * **The cap (X-5, RAIL-16).** Only the server's `409 cap_reached` sets
 * `workbenchBusy`; no number of working lanes ever does, and no cap number is
 * written here. The block lifts when a run it could be waiting on reaches a
 * terminal state, or `CAP_RECHECK_MS` after the refusal (I-13), so a cap held
 * by another tab can never lock this composer for good: the next submit asks
 * the server again, and the server enforces.
 *
 * **Staleness.** Answers can arrive out of order (a start's answer, a status
 * read and a cancel can cross). An answer replaces the held one only if it is
 * not older (`supersedes`): attempts compare as numbers and `updated_at` as an
 * instant, never as a string; a terminal state is final for its attempt; a
 * higher attempt follows only a `working` or `failed` one; and
 * `cancel_requested` never goes back to false within an attempt.
 *
 * Privacy (X-7): a brief lives in this memory and nowhere else — no storage,
 * no URL, no log. A turn's key is `live:<invocation_id>`, never its text.
 */

import { CONTRACT_VERSION } from './contracts'
import type { ErrorInfo, TimelineEntry, ToolId, ToolInvocation, ToolInvocationRequest } from './contracts'
import type { GmTurn } from './gmTimeline'
import { normaliseLane } from './laneState'
import type { LaneActionId, LaneView } from './laneState'

/** I-13 (suggested): how long a `cap_reached` refusal blocks Send at most. */
export const CAP_RECHECK_MS = 10_000
/** RAIL-21: a run this client has waited on longer than this is checked on. */
export const LOST_AFTER_MS = 120_000

export interface PendingEntry {
  /** Exactly what was sent. A retry re-sends it unchanged (RAIL-18). */
  readonly request: ToolInvocationRequest
  /** The newest server answer; `null` until the first. */
  readonly invocation: ToolInvocation | null
  /** A start the server refused before any attempt existed (never `cap_reached`). */
  readonly refusal: ErrorInfo | null
  /**
   * The last send has had no answer yet: no `answered` accepted and no
   * `refused` since the last `submitted`. A Try again is therefore a run in
   * flight — drawn working, watched, cancellable — never the failure it
   * retries (RAIL-15, RAIL-21, RAIL-23).
   */
  readonly awaiting: boolean
  /** RAIL-21: the answer was lost, or the client waited past `LOST_AFTER_MS`. */
  readonly lost: boolean
  /** RAIL-23: a cancel was asked for and no terminal state has been seen. */
  readonly cancelling: boolean
  /** When it was last sent, on the caller's clock. */
  readonly submittedAt: number
}

export interface PendingWork {
  /** By `invocation_id`. */
  readonly entries: ReadonlyMap<string, PendingEntry>
  /** Submission order. */
  readonly order: readonly string[]
  /** Until when the server's cap blocks Send, on the caller's clock. */
  readonly capBlockedUntil: number | null
  /** The runs the `cap_reached` answer named as in flight. */
  readonly capInFlight: readonly string[]
}

export type PendingEvent =
  /** A start was sent: a new run, or a Try again of a held one. */
  | { type: 'submitted'; request: ToolInvocationRequest; at: number }
  /** A start, status or cancel answer, or a GM realtime frame. */
  | { type: 'answered'; invocation: ToolInvocation }
  /** A start refused before any attempt started (an HTTP error, or a request that never left). */
  | { type: 'refused'; invocationId: string; error: ErrorInfo; at: number }
  /** The start's answer never came back, or `overdue` listed the run. */
  | { type: 'lost'; invocationId: string }
  /** The GM pressed Cancel. */
  | { type: 'cancel-requested'; invocationId: string }

export type LaneIntent =
  /** POST the request — for Try again, the held one, byte for byte. */
  | { type: 'start'; request: ToolInvocationRequest }
  /** GET the run's status by id. Never re-runs (RAIL-21). */
  | { type: 'status'; invocationId: string }
  /** POST the run's cancel. */
  | { type: 'cancel'; invocationId: string }
  /** Arm the composer with this tool and brief (RAIL-19, RAIL-22); a submit mints a new id. */
  | { type: 'arm'; toolId: ToolId; brief: string }

type ToolTurn = Extract<GmTurn, { kind: 'tool' }>
/** `contracts.ts` exports no alias for a stored tool entry; derive one here. */
type StoredToolEntry = Extract<TimelineEntry, { entry_kind: 'tool' }>

export interface LiveToolTurn {
  turn: ToolTurn
  submittedAt: number
}

const EMPTY: PendingWork = { entries: new Map(), order: [], capBlockedUntil: null, capInFlight: [] }

export function emptyPendingWork(): PendingWork {
  return EMPTY
}

function isTerminal(invocation: ToolInvocation): boolean {
  return invocation.status !== 'working'
}

/** Nothing of this run is still going as far as this client knows: no send
 * awaits an answer, and it was refused or its answer is terminal. */
function settled(entry: PendingEntry): boolean {
  if (entry.awaiting) return false
  return entry.refusal !== null || (entry.invocation !== null && isTerminal(entry.invocation))
}

/**
 * Whether `incoming` may replace `held` for the same run. Pure and total: the
 * one staleness rule, shared by the reducer and by the stored-versus-live
 * choice in `turnsWithPendingWork`.
 */
export function supersedes(held: ToolInvocation | null, incoming: ToolInvocation): boolean {
  if (held === null) return true
  if (incoming.attempt !== held.attempt) {
    // A higher attempt is a Try again of a failed run (RAIL-18), or of one the
    // server expired (RAIL-27) whose failure this client never saw. Done and
    // cancelled are final: the server starts nothing after them.
    return incoming.attempt > held.attempt && (held.status === 'working' || held.status === 'failed')
  }
  if (Date.parse(incoming.updated_at) < Date.parse(held.updated_at)) return false
  if (held.cancel_requested && !incoming.cancel_requested) return false
  // A terminal state is final for its attempt. The same state may follow it:
  // a cancel landing on a finished run flags it (RAIL-23).
  return held.status === 'working' || incoming.status === held.status
}

function withEntries(state: PendingWork, entries: Map<string, PendingEntry>, order = state.order): PendingWork {
  return { ...state, entries, order }
}

function put(state: PendingWork, id: string, entry: PendingEntry): PendingWork {
  const entries = new Map(state.entries)
  entries.set(id, entry)
  return withEntries(state, entries)
}

const CAP_CLEAR = { capBlockedUntil: null, capInFlight: [] } as const

function submitted(state: PendingWork, request: ToolInvocationRequest, at: number): PendingWork {
  const id = request.invocation_id
  const held = state.entries.get(id)
  if (held !== undefined) {
    // A Try again. The held request stays exactly as it was: a body that
    // differs is not adopted, because the key names the request (RAIL-18).
    // Its answer and refusal stay too, only hidden while `awaiting`: a
    // `cap_reached` answer to this send leaves the lane as it was (critic 16).
    return put(state, id, { ...held, submittedAt: at, lost: false, awaiting: true })
  }
  const entries = new Map(state.entries)
  entries.set(id, { request, invocation: null, refusal: null, awaiting: true, lost: false, cancelling: false, submittedAt: at })
  return withEntries(state, entries, [...state.order, id])
}

function answered(state: PendingWork, invocation: ToolInvocation): PendingWork {
  const id = invocation.invocation_id
  const held = state.entries.get(id)
  if (held === undefined) {
    // Never a turn from an answer. It only tells the cap block that a run it
    // named has ended (critic 18).
    return isTerminal(invocation) && state.capInFlight.includes(id) ? { ...state, ...CAP_CLEAR } : state
  }
  if (invocation.tool_id !== held.request.tool_id || !supersedes(held.invocation, invocation)) return state
  const newAttempt = held.invocation !== null && invocation.attempt > held.invocation.attempt
  // A new attempt is watched afresh — unless it is the one a Try again in
  // flight was waiting on: the wait and a cancel asked for it are its own.
  const sameWatch = held.awaiting || !newAttempt
  const next = put(state, id, {
    ...held,
    invocation,
    refusal: null,
    awaiting: false,
    lost: held.lost && sameWatch,
    cancelling: held.cancelling && sameWatch && !isTerminal(invocation),
  })
  return isTerminal(invocation) ? { ...next, ...CAP_CLEAR } : next
}

function refused(state: PendingWork, id: string, error: ErrorInfo, at: number): PendingWork {
  const held = state.entries.get(id)
  if (held === undefined) return state
  // A refusal answers the send, whatever else it changes.
  const heard = { ...held, awaiting: false }
  if (error.code === 'cap_reached') {
    const blocked = { ...state, capBlockedUntil: at + CAP_RECHECK_MS, capInFlight: error.in_flight ?? [] }
    // A first submit that never got anything leaves no lane: its draft is the
    // controller's to restore. A Try again keeps the lane it was sent from —
    // a failed run's, or a refused start's (critic 16).
    if (held.invocation !== null || held.refusal !== null) return put(blocked, id, heard)
    const entries = new Map(state.entries)
    entries.delete(id)
    return withEntries(blocked, entries, state.order.filter((other) => other !== id))
  }
  // A refusal ends only a start: over a run that is working, done or
  // cancelled it is not the lane's news.
  if (held.invocation !== null && held.invocation.status !== 'failed') return held.awaiting ? put(state, id, heard) : state
  return put(state, id, { ...heard, refusal: error, cancelling: false })
}

function markLost(state: PendingWork, id: string): PendingWork {
  const held = state.entries.get(id)
  if (held === undefined || held.lost || settled(held)) return state
  return put(state, id, { ...held, lost: true })
}

function requestCancel(state: PendingWork, id: string): PendingWork {
  const held = state.entries.get(id)
  if (held === undefined || held.cancelling || settled(held)) return state
  return put(state, id, { ...held, cancelling: true })
}

/** The one way the model changes. Returns `state` itself when nothing did. */
export function reducePendingWork(state: PendingWork, event: PendingEvent): PendingWork {
  switch (event.type) {
    case 'submitted':
      return submitted(state, event.request, event.at)
    case 'answered':
      return answered(state, event.invocation)
    case 'refused':
      return refused(state, event.invocationId, event.error, event.at)
    case 'lost':
      return markLost(state, event.invocationId)
    case 'cancel-requested':
      return requestCancel(state, event.invocationId)
  }
}

/** RAIL-21: the runs waited on past `LOST_AFTER_MS`, in submission order, that
 * are not already being checked on. The caller marks each `lost`. */
export function overdue(state: PendingWork, now: number): string[] {
  return state.order.filter((id) => {
    const entry = state.entries.get(id)
    return entry !== undefined && !entry.lost && !settled(entry) && now - entry.submittedAt > LOST_AFTER_MS
  })
}

/** `ToolComposer`'s two props, exactly (RAIL-16): a plain turn in flight, or
 * the server's cap. A working lane is neither. */
export function composerGate(
  state: PendingWork,
  o: { chatInFlight: boolean; now: number },
): { chatPending: boolean; workbenchBusy: boolean } {
  return {
    chatPending: o.chatInFlight,
    workbenchBusy: state.capBlockedUntil !== null && o.now < state.capBlockedUntil,
  }
}

function instant(ms: number): string {
  const date = new Date(ms)
  return Number.isFinite(date.getTime()) ? date.toISOString() : new Date(0).toISOString()
}

/**
 * The invocation a lane is drawn from — for rendering only, never stored and
 * never compared, so a client clock can never beat a server `updated_at`. The
 * fresher of the stored copy and the held one; a refusal as a failed run; a
 * run with no answer yet as working; a Try again in flight as working, as the
 * attempt it starts (so `Still working…` times it afresh), never as the
 * failure it retries; and a cancel asked for as `cancel_requested` until the
 * server says how it ended (RAIL-23).
 */
function drawnInvocation(entry: PendingEntry, stored: ToolInvocation | null = null): ToolInvocation {
  const held = stored !== null && supersedes(entry.invocation, stored) ? stored : entry.invocation
  const base: ToolInvocation = held ?? {
    schema_version: CONTRACT_VERSION,
    invocation_id: entry.request.invocation_id,
    tool_id: entry.request.tool_id,
    status: 'working',
    attempt: 1,
    cancel_requested: false,
    created_at: instant(entry.submittedAt),
    updated_at: instant(entry.submittedAt),
    result: null,
    error: null,
  }
  if (entry.awaiting) {
    if (base.status === 'failed') {
      return { ...base, status: 'working', attempt: base.attempt + 1, cancel_requested: entry.cancelling, result: null, error: null }
    }
  } else if (entry.refusal !== null && (held === null || held.status === 'failed')) {
    return { ...base, status: 'failed', result: null, error: entry.refusal }
  }
  if (entry.cancelling && base.status === 'working') return { ...base, cancel_requested: true }
  return base
}

function laneOf(entry: PendingEntry, stored: ToolInvocation | null): LaneView {
  return normaliseLane(drawnInvocation(entry, stored), { lost: entry.lost })
}

/**
 * What a lane action asks the controller to do — or `null` when the lane this
 * model draws for that run does not offer the action (critic 17), or the model
 * does not hold the run. Nothing here returns a `start` except Try again, and
 * Try again returns the held request unchanged: same id, same brief, same ids
 * (RAIL-18, X-1).
 *
 * `stored` is the run's stored copy when the thread holds one — the turn's
 * invocation as `turnsWithPendingWork` was given it — so the answer is for the
 * lane as drawn, which may be that fresher copy.
 */
export function intentFor(
  state: PendingWork,
  invocationId: string,
  action: LaneActionId,
  stored: ToolInvocation | null = null,
): LaneIntent | null {
  const entry = state.entries.get(invocationId)
  if (entry === undefined) return null
  const view = laneOf(entry, stored)
  if (view.state === 'done' || view.state === 'unreadable' || !view.actions.includes(action)) return null
  switch (action) {
    case 'try-again':
      return { type: 'start', request: entry.request }
    case 'check-again':
      return { type: 'status', invocationId }
    case 'cancel':
      return { type: 'cancel', invocationId }
    case 'edit-brief':
    case 'run-again':
      return { type: 'arm', toolId: entry.request.tool_id, brief: entry.request.brief }
  }
}

/**
 * The request a stored run was started with, rebuilt after a reload so Try
 * again re-sends it under its own id (RAIL-18). `conversationCampaignId` is
 * the conversation's own campaign, never the selector's: the server scopes an
 * idempotency key to the GM and the campaign, so another campaign would start
 * a fresh, billable run (X-1).
 */
export function requestFromEntry(
  entry: StoredToolEntry,
  o: { conversationCampaignId: string; conversationId: string },
): ToolInvocationRequest {
  return {
    schema_version: CONTRACT_VERSION,
    invocation_id: entry.invocation.invocation_id,
    tool_id: entry.invocation.tool_id,
    brief: entry.brief,
    campaign_id: o.conversationCampaignId,
    conversation_id: o.conversationId,
    ...(typeof entry.source_entry_id === 'string' ? { source_entry_id: entry.source_entry_id } : {}),
  }
}

/**
 * The thread with this conversation's pending work drawn in (critic 20).
 * `stored` is the hydrated turns only — never `turnFromExchange` output — so
 * where a live tool turn sits among live chat turns stays `1kg.4.5`'s call.
 *
 * - `turns`: every stored turn in place. A stored tool turn whose run the
 *   model holds is drawn once, where it is stored, from the fresher of the two
 *   invocations, and marked `live`.
 * - `liveTools`: the held runs of this conversation that are not stored yet,
 *   in submission order, keyed `live:<invocation_id>`.
 */
export function turnsWithPendingWork(
  stored: readonly GmTurn[],
  state: PendingWork,
  conversationId: string,
): { turns: GmTurn[]; liveTools: LiveToolTurn[] } {
  const drawn = new Set<string>()
  const turns = stored.map((turn): GmTurn => {
    if (turn.kind !== 'tool') return turn
    const id = turn.invocation.invocation_id
    const entry = state.entries.get(id)
    if (entry === undefined || entry.request.conversation_id !== conversationId) return turn
    drawn.add(id)
    return { ...turn, invocation: drawnInvocation(entry, turn.invocation), live: { lost: entry.lost } }
  })
  const liveTools: LiveToolTurn[] = []
  for (const id of state.order) {
    const entry = state.entries.get(id)
    if (entry === undefined || entry.request.conversation_id !== conversationId || drawn.has(id)) continue
    liveTools.push({
      turn: {
        kind: 'tool',
        key: `live:${id}`,
        entryId: null,
        brief: entry.request.brief,
        invocation: drawnInvocation(entry),
        live: { lost: entry.lost },
      },
      submittedAt: entry.submittedAt,
    })
  }
  return { turns, liveTools }
}
