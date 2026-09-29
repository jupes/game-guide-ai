/**
 * tableSession -- the GM's live table for the selected campaign
 * (agent-forge-harness-1kg.2.5.3; brief section 7.9, Critic item 23).
 *
 * `TableSessionProvider` follows the campaign context's scope
 * (`useCampaign().scope`), so it inherits that context's `dm` gate and its
 * account keying: no scope, idle, no request. What it shows is stored with the
 * (account, campaign, key) it was requested under and renders only while that
 * is current (T4-6). Start is one press, never retried by the client, with one
 * command id per intent kept until a definitive answer (T4-1); `live_elsewhere`
 * never triggers an End (DV-6, TA-7(5), T4-4); a 403 offers no Retry and goes
 * to `onStartRefused` (`ecr`). End is offered whenever a live session is known,
 * whatever is pending, for any tier (X-3, T4-3), and delivered by an
 * `EndCourier` that outlives the scope and this provider. One status re-read
 * when `ends_at` passes, never a poll, and no `liveSession` past it whatever
 * that re-read says (SEC-42, T4-7). No Rotate, no screen UI,
 * no timeline re-read, no web storage.
 */

import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'
import type { TableSession } from '../gm/contracts'
import { useCampaign, type CampaignScope } from './campaignContext'
import { CurrentUserContext } from './currentUser'
import {
  Emitter,
  EndCourier,
  MAX_TIMER_MS,
  mintCommandId,
  readTableSession,
  sendTableSession,
  type EndOutcome,
} from './tableSessionApi'

export type TableSessionState = 'idle' | 'loading' | 'none' | 'live' | 'ended' | 'failed'

/** `load_failed`, `start_failed` and `throttled` offer Retry (`retry()`); the rest do not. */
export type SessionProblem =
  | { readonly kind: 'load_failed' | 'unavailable' | 'start_failed' | 'start_refused' | 'live_elsewhere' }
  | { readonly kind: 'throttled'; readonly retryAfterS: number | null }

export interface TableSessionView {
  readonly sessionId: string
  readonly campaignId: string
  readonly state: 'live' | 'ended'
  readonly startedAt: string
  readonly endsAt: string
  readonly endedAt: string | null
}

/** For `1ir` and `1kg.7`: non-null only while the session is live and its
 * `ends_at` has not yet passed on this clock (SEC-42, T4-7). */
export interface LiveSession {
  readonly sessionId: string
  readonly campaignId: string
  readonly revealEpoch: number
  readonly audioEpoch: number
}

/** `skipped`: nothing sent. `stale`: the scope moved in flight; the answer was dropped. */
export type StartOutcome = 'started' | 'failed' | 'refused' | 'unavailable' | 'live_elsewhere' | 'throttled' | 'signed_out' | 'skipped' | 'stale'

export interface TableSessionValue {
  readonly state: TableSessionState
  readonly session: TableSessionView | null
  readonly pending: 'start' | 'end' | null
  /** An End failed and waits to retry; pressing End sends it now. */
  readonly endRetrying: boolean
  readonly problem: SessionProblem | null
  readonly liveSession: LiveSession | null
  start(): Promise<StartOutcome>
  end(): Promise<EndOutcome['kind'] | 'skipped'>
  retry(): Promise<StartOutcome>
}

type Refused = ((code: string | null) => void) | undefined

interface Snap {
  readonly token: string | null
  readonly read: 'loading' | 'ready' | 'failed'
  readonly session: TableSession | null
  readonly starting: boolean
  readonly startCommand: string | null
  readonly problem: SessionProblem | null
  /** `session_id@ends_at`, once re-read at expiry. */
  readonly expiryReadFor: string | null
}

/** A live session is past its `ends_at` once the timer for this mark has fired. */
const expiryMark = (session: TableSession): string => `${session.session_id}@${session.ends_at}`

const EMPTY: Snap = { token: null, read: 'loading', session: null, starting: false, startCommand: null, problem: null, expiryReadFor: null }

class SessionStore extends Emitter {
  private snap: Snap = EMPTY

  getSnapshot = (): Snap => this.snap

  /** Applies only while `token` is still current. */
  private patch(token: string, patch: Partial<Snap>): void {
    if (this.snap.token !== token) return
    this.snap = { ...this.snap, ...patch }
    this.emit()
  }

  /** A new scope reads once; the same one again is a no-op (StrictMode); none forgets. */
  enter(token: string | null, campaignId: string | null, fetchImpl: typeof fetch): void {
    if (token === this.snap.token) return
    this.snap = { ...EMPTY, token }
    this.emit()
    if (token !== null && campaignId !== null) void this.read(token, campaignId, fetchImpl)
  }

  private async read(token: string, campaignId: string, fetchImpl: typeof fetch): Promise<void> {
    const result = await readTableSession(campaignId, fetchImpl)
    if (result.kind === 'ok') this.patch(token, { read: 'ready', session: result.session, problem: null })
    else if (result.kind === 'unavailable' || result.kind === 'refused') this.patch(token, { read: 'failed', problem: { kind: 'unavailable' } })
    else if (result.kind !== 'unauthorized') this.patch(token, { read: 'failed', problem: { kind: 'load_failed' } })
  }

  expire(token: string, fetchImpl: typeof fetch): void {
    const { session } = this.snap
    const mark = session === null ? null : expiryMark(session)
    if (this.snap.token !== token || session?.state !== 'live' || this.snap.expiryReadFor === mark) return
    this.patch(token, { expiryReadFor: mark, read: 'loading' })
    void this.read(token, session.campaign_id, fetchImpl)
  }

  async start(token: string, campaignId: string, fetchImpl: typeof fetch, onRefused: Refused): Promise<StartOutcome> {
    const now = this.snap
    if (now.token !== token || now.starting || now.session?.state === 'live') return 'skipped'
    const commandId = now.startCommand ?? mintCommandId()
    // The problem stays until the answer, so the control pressed stays mounted.
    this.patch(token, { starting: true, startCommand: commandId })
    const result = await sendTableSession(campaignId, { action: 'start', commandId }, fetchImpl)
    if (this.snap.token !== token) return 'stale'
    const done = (outcome: StartOutcome, patch: Partial<Snap>): StartOutcome => {
      this.patch(token, { starting: false, ...patch })
      return outcome
    }
    switch (result.kind) {
      case 'ok':
        return done('started', { startCommand: null, session: result.session, read: 'ready', problem: null })
      case 'refused':
        onRefused?.(result.code)
        return done('refused', { startCommand: null, problem: { kind: 'start_refused' } })
      case 'unavailable':
      case 'live_elsewhere':
        return done(result.kind, { startCommand: null, problem: { kind: result.kind } })
      case 'throttled':
        return done('throttled', { problem: { kind: 'throttled', retryAfterS: result.retryAfterS } })
      case 'unauthorized':
        return done('signed_out', {})
      default:
        return done('failed', { problem: { kind: 'start_failed' } })
    }
  }

  retry(token: string, campaignId: string, fetchImpl: typeof fetch, onRefused: Refused): Promise<StartOutcome> {
    const { problem, read } = this.snap
    if (this.snap.token !== token) return skipped()
    if (problem?.kind === 'start_failed' || problem?.kind === 'throttled') return this.start(token, campaignId, fetchImpl, onRefused)
    if (problem?.kind === 'load_failed' && read !== 'loading') {
      this.patch(token, { read: 'loading' })
      void this.read(token, campaignId, fetchImpl)
    }
    return skipped()
  }

  /** An End's answer lands on whichever scope now shows that session. */
  ended(sessionId: string, outcome: EndOutcome): void {
    const { token, session } = this.snap
    if (token === null || session?.session_id !== sessionId) return
    if (outcome.kind === 'gone') this.patch(token, { session: null, read: 'ready' })
    if (outcome.kind !== 'ended') return
    const stood: TableSession = outcome.session ?? { ...session, state: 'ended', ended_at: new Date().toISOString(), screens: [] }
    this.patch(token, { session: stood, read: 'ready', problem: null })
  }
}

function skipped(): Promise<'skipped'> {
  return Promise.resolve('skipped')
}

const INERT: TableSessionValue = {
  state: 'idle',
  session: null,
  pending: null,
  endRetrying: false,
  problem: null,
  liveSession: null,
  start: skipped,
  end: skipped,
  retry: skipped,
}
const SHARED_COURIER = new EndCourier()
const TableSessionContext = createContext<TableSessionValue | null>(null)

export interface TableSessionProviderProps {
  /** Omitted, as the app mounts it: `useCampaign().scope`. A host holding a
   * scope of its own (a test) passes it; `null` is no campaign. */
  readonly scope?: CampaignScope | null
  readonly children?: ReactNode
  readonly fetchImpl?: typeof fetch
  /** A 403 on Start, with its code; `ecr` designs the answer (Critic 23c). */
  readonly onStartRefused?: (code: string | null) => void
  /** One per page by default, so an End outlives this provider. */
  readonly courier?: EndCourier
}

export function TableSessionProvider({
  scope: given,
  children,
  fetchImpl = fetch,
  onStartRefused,
  courier = SHARED_COURIER,
}: TableSessionProviderProps): React.JSX.Element {
  const selected = useCampaign().scope
  const scope = given === undefined ? selected : given
  const userId = useContext(CurrentUserContext)?.user.id ?? null
  const campaignId = scope?.campaignId ?? null
  const token = scope === null ? null : JSON.stringify([userId, scope.campaignId, scope.key])
  const [store] = useState(() => new SessionStore())
  const snap = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const current = token !== null && snap.token === token ? snap : null
  const known = current?.session ?? null
  const ending = useSyncExternalStore(courier.subscribe, () => (known === null ? 'idle' : courier.status(known.session_id)))

  useEffect(() => store.enter(token, campaignId, fetchImpl), [store, token, campaignId, fetchImpl])

  const liveEndsAt = known?.state === 'live' ? known.ends_at : null
  useEffect(() => {
    if (token === null || liveEndsAt === null) return undefined
    const delay = Math.min(Math.max(Date.parse(liveEndsAt) - Date.now(), 0), MAX_TIMER_MS)
    const timer = setTimeout(() => store.expire(token, fetchImpl), delay)
    return () => clearTimeout(timer)
  }, [store, token, liveEndsAt, fetchImpl])

  const value = useMemo<TableSessionValue>(() => {
    if (token === null || campaignId === null) return INERT
    const shown = current ?? { ...EMPTY, token }
    const state = shown.read === 'failed' ? 'failed' : (known?.state ?? (shown.read === 'loading' ? 'loading' : 'none'))
    const live = state === 'live' && known !== null && shown.expiryReadFor !== expiryMark(known)
    return {
      state,
      session: known && {
        sessionId: known.session_id,
        campaignId: known.campaign_id,
        state: known.state,
        startedAt: known.started_at,
        endsAt: known.ends_at,
        endedAt: known.ended_at,
      },
      pending: shown.starting ? 'start' : ending !== 'idle' ? 'end' : null,
      endRetrying: ending === 'waiting',
      problem: shown.problem,
      liveSession: live
        ? { sessionId: known.session_id, campaignId: known.campaign_id, revealEpoch: known.reveal_epoch, audioEpoch: known.audio_epoch }
        : null,
      start: () => store.start(token, campaignId, fetchImpl, onStartRefused),
      end: () => {
        if (known?.state !== 'live') return skipped()
        return courier.send(known.campaign_id, known.session_id, fetchImpl).then((outcome) => {
          store.ended(known.session_id, outcome)
          return outcome.kind
        })
      },
      retry: () => store.retry(token, campaignId, fetchImpl, onStartRefused),
    }
  }, [token, campaignId, current, known, ending, store, courier, fetchImpl, onStartRefused])

  return <TableSessionContext.Provider value={value}>{children}</TableSessionContext.Provider>
}

/** Outside a provider: an inert, idle value that never fetches (never throws). */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useTableSession(): TableSessionValue {
  return useContext(TableSessionContext) ?? INERT
}
