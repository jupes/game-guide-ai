/**
 * revealContext -- the GM's reveal picture for the selected campaign
 * (agent-forge-harness-1kg.7.3; brief 3.2 and the Critic's items 3, 4, 8, 9, 14, 16).
 *
 * `RevealProvider` follows the campaign context's scope (`useCampaign().scope`), so it
 * inherits that context's `dm` gate and its account keying: no scope, idle, no request.
 * Everything it holds is stored with the (account, campaign, key) it was requested
 * under and shown only while that is current, so a scope change clears the picture,
 * the sheet and the announcement in the same render and an answer for an old scope
 * is dropped.
 *
 * REVEAL-13: the picture is the server's, and a body that does not parse is `unknown`,
 * never "nothing revealed". Ordering (Critic 4): every request takes a send sequence.
 * Within one session the higher reveal epoch wins and a tie goes to the later-sent
 * request; across sessions, or for a `null` answer, the later-sent request wins. So a
 * late Confirm can never overwrite a later Stop's picture, and a late answer from an
 * ended session can never overwrite the new one.
 *
 * A Confirm is sent once, never retried (REVEAL-22), and is not sent at all while a
 * Stop is unacknowledged. A Stop goes through the `StopCourier` at once and is
 * retried until the server has answered for it; it outlives a scope change.
 *
 * No web storage, no URL, no realtime channel (1kg.7.5): the picture is re-read on
 * scope entry, when the table session's id or epoch moves, when the sheet opens,
 * after a refusal, and on a bounded backoff while the read keeps failing.
 */

import * as React from 'react'
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'
import type { RevealRequest, RevealState, Seat } from '../gm/contracts'
import { REVEAL_COPY } from '../gm/revealCopy'
import { confirmReveal, readReveals, type ConfirmResult } from '../gm/revealApi'
import { liveOf, sameAudience, type DraftAudience } from '../gm/revealFields'
import { listCampaignSeats } from './campaignApi'
import { useCampaign, type CampaignScope } from './campaignContext'
import { useCanvasState, useWorkbenchActive } from './canvasContext'
import { CurrentUserContext } from './currentUser'
import { STOP_ALL, StopCourier } from './revealStop'
import { Emitter } from './tableSessionApi'
import { useTableSession } from './tableSession'

export type RevealStatus = 'idle' | 'loading' | 'none' | 'live' | 'unknown'

/** A Confirm's outcome as the store reports it. `ok` says whether the answered picture really holds what was asked (Critic 9). */
export type ConfirmOutcome =
  | Exclude<ConfirmResult, { readonly kind: 'ok' }>
  | {
      readonly kind: 'ok'
      readonly state: RevealState | null
      /** `held`: the picture holds the document live, to the audience and mask asked. `changed`: it does not (a replay after a Stop, say). `stopped`: a Stop was pressed for it since. */
      readonly result: 'held' | 'changed' | 'stopped'
    }

export interface RevealsValue {
  /** `idle` = no dm scope. `none` = the server says no live session. */
  readonly status: RevealStatus
  readonly state: RevealState | null
  /** Document ids with an unacknowledged Stop (`*` for all), in this campaign. */
  readonly stopping: ReadonlySet<string>
  /** A Stop failed at least once and is retrying, or was refused outright. */
  readonly stopFailed: boolean
  readonly sheet: { readonly documentId: string } | null
  /** What had focus when the sheet was opened, for the return of focus on close. Stable for the provider's life. */
  readonly openerRef: React.RefObject<Element | null>
  /** The seats known so far, or `null` until read. The header names a private audience with them. */
  readonly seats: readonly Seat[] | null
  /** `''` until an outcome. */
  readonly announcement: string
  /** Changes with every announcement, so an identical message is announced again. */
  readonly announcementTick: number
  /** `opener` is whatever had focus at the gesture. */
  openSheet(documentId: string, opener?: Element | null): void
  closeSheet(): void
  /** The sheet read the seats; the header names them from now on. */
  noteSeats(seats: readonly Seat[]): void
  /** Speaks an outcome in the one status node (`RevealAnnouncer`). */
  announce(text: string): void
  refresh(): Promise<void>
  confirm(request: Omit<RevealRequest, 'schema_version' | 'command_id'>, commandId: string): Promise<ConfirmOutcome>
  /** Fire-and-forget; courier-delivered. `title` is only for the spoken outcome. */
  stop(documentId: string, title?: string): void
}

// ── The store ────────────────────────────────────────────────────────────────

/** The wait before the first, second, ... re-read of a picture that could not be read; the last repeats. */
const READ_BACKOFF_MS = [2_000, 4_000, 8_000, 16_000, 30_000]
const NBSP = ' '

interface Snap {
  readonly token: string | null
  readonly read: 'loading' | 'ready' | 'unknown'
  readonly state: RevealState | null
  readonly sheet: { readonly documentId: string } | null
  readonly seats: readonly Seat[] | null
  readonly announcement: string
  readonly announcementTick: number
  /** Documents whose Stop the server refused outright. Cleared by the next press for the document. */
  readonly refused: ReadonlySet<string>
}

const NONE: ReadonlySet<string> = new Set()

const EMPTY: Snap = {
  token: null, read: 'loading', state: null, sheet: null, seats: null, announcement: '', announcementTick: 0, refused: NONE,
}

class RevealStore extends Emitter {
  private snap: Snap = EMPTY
  /** The seats are asked for once per scope on the header's account; the sheet reads them itself when it opens. */
  private seatsAsked = false
  private campaignId: string | null = null
  private fetchImpl: typeof fetch = fetch
  /** Every request takes the next number when it is SENT. */
  private sendSeq = 0
  /** The send number of the request whose picture (or failure) is held. */
  private heldSeq = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private failures = 0
  /** The send number of the latest Stop pressed, per document (and `*`). */
  private readonly stopPressed = new Map<string, number>()

  getSnapshot = (): Snap => this.snap

  private patch(token: string, patch: Partial<Snap>): void {
    if (this.snap.token !== token) return
    this.snap = { ...this.snap, ...patch }
    this.emit()
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer)
    this.retryTimer = null
  }

  /** A new scope reads once; the same one again is a no-op (StrictMode); none forgets. */
  enter(token: string | null, campaignId: string | null, fetchImpl: typeof fetch): void {
    if (token === this.snap.token) return
    this.clearRetry()
    this.failures = 0
    this.heldSeq = 0
    this.stopPressed.clear()
    this.seatsAsked = false
    this.campaignId = campaignId
    this.fetchImpl = fetchImpl
    this.snap = { ...EMPTY, token }
    this.emit()
    if (token !== null && campaignId !== null) void this.read(token, campaignId, fetchImpl)
  }

  dispose(): void {
    this.clearRetry()
  }

  /** Whether an answer sent as `seq` may replace what is held (Critic 4). */
  private wins(incoming: RevealState | null, seq: number): boolean {
    const held = this.snap.state
    if (incoming !== null && held !== null && incoming.session_id === held.session_id) {
      if (incoming.reveal_epoch !== held.reveal_epoch) return incoming.reveal_epoch > held.reveal_epoch
    }
    return seq > this.heldSeq
  }

  private apply(token: string, seq: number, incoming: RevealState | null): boolean {
    if (this.snap.token !== token || !this.wins(incoming, seq)) return false
    this.heldSeq = Math.max(this.heldSeq, seq)
    this.failures = 0
    this.clearRetry()
    this.patch(token, { read: 'ready', state: incoming })
    this.ensureSeats(token, incoming)
    return true
  }

  /** A document live to a participant needs the seats to be named in the header; read them once. */
  private ensureSeats(token: string, picture: RevealState | null): void {
    const privately = picture?.slots.some((entry) => entry.live !== null && entry.slot.kind === 'participant') === true
    if (!privately || this.seatsAsked || this.snap.seats !== null || this.campaignId === null) return
    this.seatsAsked = true
    void listCampaignSeats(this.campaignId, this.fetchImpl).then((result) => {
      if (result.kind === 'ok') this.noteSeats(token, result.items)
    })
  }

  noteSeats(token: string, seats: readonly Seat[]): void {
    this.patch(token, { seats })
  }

  async read(token: string, campaignId: string, fetchImpl: typeof fetch): Promise<void> {
    const seq = ++this.sendSeq
    const result = await readReveals(campaignId, fetchImpl)
    if (this.snap.token !== token) return
    if (result.kind === 'ok') {
      this.apply(token, seq, result.state)
      return
    }
    if (result.kind === 'unauthorized' || seq <= this.heldSeq) return
    // Fail closed (REVEAL-13): a picture that could not be read is unknown, never "nothing revealed".
    this.heldSeq = seq
    this.patch(token, { read: 'unknown' })
    // Only an outage is worth polling; a scope that is not this GM's, or is gone, is read again on the other triggers (Critic 16).
    if (result.kind === 'failed') this.scheduleRetry(token, campaignId, fetchImpl)
  }

  private scheduleRetry(token: string, campaignId: string, fetchImpl: typeof fetch): void {
    this.clearRetry()
    const wait = READ_BACKOFF_MS[Math.min(this.failures, READ_BACKOFF_MS.length - 1)]
    this.failures += 1
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      if (this.snap.token === token) void this.read(token, campaignId, fetchImpl)
    }, wait)
  }

  /** The table session's id or epoch moved: read again unless the held picture already matches. */
  sync(token: string, campaignId: string, live: { sessionId: string; revealEpoch: number } | null, fetchImpl: typeof fetch): void {
    if (this.snap.token !== token) return
    const { state, read } = this.snap
    if (live === null) return
    if (read === 'ready' && state !== null && state.session_id === live.sessionId && state.reveal_epoch >= live.revealEpoch) return
    void this.read(token, campaignId, fetchImpl)
  }

  openSheet(token: string, campaignId: string, documentId: string, fetchImpl: typeof fetch): void {
    this.patch(token, { sheet: { documentId } })
    void this.read(token, campaignId, fetchImpl)
  }

  closeSheet(token: string): void {
    if (this.snap.sheet !== null) this.patch(token, { sheet: null })
  }

  announce(token: string, text: string): void {
    this.patch(token, { announcement: text, announcementTick: this.snap.announcementTick + 1 })
  }

  async confirm(
    token: string,
    campaignId: string,
    request: Omit<RevealRequest, 'schema_version' | 'command_id'>,
    commandId: string,
    blocked: boolean,
    fetchImpl: typeof fetch,
  ): Promise<ConfirmOutcome> {
    // REVEAL-22: a widening is never sent while a narrowing is unacknowledged.
    if (blocked) return { kind: 'blocked' }
    const seq = ++this.sendSeq
    const result = await confirmReveal(campaignId, { ...request, command_id: commandId }, fetchImpl)
    if (result.kind !== 'ok') {
      // The picture the sheet showed is out of date: read it again. The caller keeps the draft and asks the GM to confirm again.
      if (['conflict', 'mask_refused', 'refused'].includes(result.kind)) void this.read(token, campaignId, fetchImpl)
      return result
    }
    this.apply(token, seq, result.state)
    const stoppedSince = (this.stopPressed.get(request.document_id) ?? 0) > seq || (this.stopPressed.get(STOP_ALL) ?? 0) > seq
    if (stoppedSince) return { kind: 'ok', state: result.state, result: 'stopped' }
    return { kind: 'ok', state: result.state, result: holds(result.state, request) ? 'held' : 'changed' }
  }

  /** A Stop was pressed: remember when, so a Confirm sent before it announces nothing. */
  pressed(documentId: string): void {
    this.stopPressed.set(documentId, ++this.sendSeq)
    if (this.snap.refused.has(documentId)) {
      const refused = new Set(this.snap.refused)
      refused.delete(documentId)
      if (this.snap.token !== null) this.patch(this.snap.token, { refused })
    }
  }

  /** A Stop's answer for the scope that pressed it. One that cannot win over what is held means the picture is stale: read it again. */
  stopped(token: string, campaignId: string, seq: number, state: RevealState | null, fetchImpl: typeof fetch): void {
    if (!this.apply(token, seq, state)) void this.read(token, campaignId, fetchImpl)
  }

  /** The server refused the Stop request itself: the table may still see it. */
  stopRefused(token: string, documentId: string): void {
    this.patch(token, { refused: new Set([...this.snap.refused, documentId]) })
  }

  stopSeq(documentId: string): number {
    return this.stopPressed.get(documentId) ?? 0
  }
}

/** Whether `state` holds the document live to the audience and the mask a Confirm asked for (Critic 9). */
function holds(state: RevealState | null, request: Omit<RevealRequest, 'schema_version' | 'command_id'>): boolean {
  const live = liveOf(state, request.document_id)
  // `everyone_seated` is never sent by this client (INFERRED I-4), so it is never "held".
  if (live === null || request.audience.kind === 'everyone_seated') return false
  const asked: DraftAudience =
    request.audience.kind === 'participants' ? { kind: 'participants', ids: request.audience.participant_ids } : { kind: 'table' }
  return (
    sameAudience(live.audience, asked) &&
    live.mask.length === request.mask.length &&
    live.mask.every((key) => request.mask.includes(key))
  )
}

// ── The provider ─────────────────────────────────────────────────────────────

const SHARED_COURIER = new StopCourier()

const NO_STOPS: ReadonlySet<string> = new Set()
const skipped = (): Promise<void> => Promise.resolve()

const INERT: RevealsValue = {
  status: 'idle',
  state: null,
  stopping: NO_STOPS,
  stopFailed: false,
  sheet: null,
  openerRef: { current: null },
  seats: null,
  announcement: '',
  announcementTick: 0,
  openSheet: () => undefined,
  closeSheet: () => undefined,
  noteSeats: () => undefined,
  announce: () => undefined,
  refresh: skipped,
  confirm: () => Promise.resolve({ kind: 'blocked' }),
  stop: () => undefined,
}

const RevealContext = createContext<RevealsValue | null>(null)

export interface RevealProviderProps {
  /** Omitted, as the app mounts it: `useCampaign().scope`. A host holding a scope of its own (a test) passes it. */
  readonly scope?: CampaignScope | null
  readonly children?: ReactNode
  readonly fetchImpl?: typeof fetch
  /** One per page by default, so a Stop outlives this provider. */
  readonly courier?: StopCourier
}

/** Nested inside another it passes its children through: two stores would be two pictures. */
export function RevealProvider(props: RevealProviderProps): React.JSX.Element {
  const parent = useContext(RevealContext)
  if (parent !== null) return <>{props.children}</>
  return <RevealProviderRoot {...props} />
}

function RevealProviderRoot({ scope: given, children, fetchImpl = fetch, courier = SHARED_COURIER }: RevealProviderProps): React.JSX.Element {
  const selected = useCampaign().scope
  const scope = given === undefined ? selected : given
  const userId = useContext(CurrentUserContext)?.user.id ?? null
  const campaignId = scope?.campaignId ?? null
  const token = scope === null ? null : JSON.stringify([userId, scope.campaignId, scope.key])
  const [store] = useState(() => new RevealStore())
  const openerRef = React.useRef<Element | null>(null)
  const snap = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const current = token !== null && snap.token === token ? snap : null
  const courierVersion = useSyncExternalStore(courier.subscribe, () => courier.version)
  const liveSession = useTableSession().liveSession
  const liveSessionId = liveSession?.sessionId ?? null
  const liveEpoch = liveSession?.revealEpoch ?? null

  useEffect(() => store.enter(token, campaignId, fetchImpl), [store, token, campaignId, fetchImpl])
  useEffect(() => () => store.dispose(), [store])
  useEffect(() => courier.retain(userId), [courier, userId])
  useEffect(() => {
    if (token === null || campaignId === null) return
    store.sync(token, campaignId, liveSessionId === null || liveEpoch === null ? null : { sessionId: liveSessionId, revealEpoch: liveEpoch }, fetchImpl)
  }, [store, token, campaignId, liveSessionId, liveEpoch, fetchImpl])

  const { stopping, failing } = useMemo(
    () => ({
      stopping: campaignId === null ? NO_STOPS : new Set(courier.pending(campaignId)),
      failing: campaignId !== null && courier.failing(campaignId),
    }),
    // `courierVersion` is the courier's change counter: it is what makes both reads current.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the version is the dependency
    [courier, campaignId, courierVersion],
  )

  // The first failure of a Stop is spoken once, as the courier starts to retry it (REVEAL-16).
  const spoke = React.useRef(false)
  useEffect(() => {
    if (token === null) return
    if (failing && !spoke.current) store.announce(token, REVEAL_COPY.stopRetrying)
    spoke.current = failing
  }, [store, token, failing])

  const value = useMemo<RevealsValue>(() => {
    if (token === null || campaignId === null) return INERT
    const shown = current ?? { ...EMPTY, token }
    const status: RevealStatus =
      shown.read === 'loading' ? 'loading' : shown.read === 'unknown' ? 'unknown' : shown.state === null ? 'none' : 'live'
    return {
      status,
      state: shown.state,
      stopping,
      stopFailed: failing || shown.refused.size > 0,
      sheet: shown.sheet,
      openerRef,
      seats: shown.seats,
      announcement: shown.announcement,
      announcementTick: shown.announcementTick,
      openSheet: (documentId, opener) => {
        openerRef.current = opener ?? null
        store.openSheet(token, campaignId, documentId, fetchImpl)
      },
      closeSheet: () => store.closeSheet(token),
      noteSeats: (seats) => store.noteSeats(token, seats),
      announce: (text) => store.announce(token, text),
      refresh: () => store.read(token, campaignId, fetchImpl),
      confirm: (request, commandId) => store.confirm(token, campaignId, request, commandId, stopping.size > 0, fetchImpl),
      stop: (documentId, title) => {
        store.pressed(documentId)
        const seq = store.stopSeq(documentId)
        void courier.send(campaignId, documentId, fetchImpl, userId).then((outcome) => {
          if (outcome.kind === 'stopped') {
            store.stopped(token, campaignId, seq, outcome.state, fetchImpl)
            store.announce(token, REVEAL_COPY.stopped(title ?? REVEAL_COPY.thisDocument))
          } else if (outcome.kind === 'gone') {
            void store.read(token, campaignId, fetchImpl)
          } else if (outcome.kind === 'invalid') {
            store.stopRefused(token, documentId)
            store.announce(token, REVEAL_COPY.stopInvalid(title ?? REVEAL_COPY.thisDocument))
          }
        })
      },
    }
  }, [token, campaignId, current, stopping, failing, store, courier, fetchImpl, userId, openerRef])

  return <RevealContext.Provider value={value}>{children}</RevealContext.Provider>
}

/** Outside a provider: an inert, idle value that never fetches (never throws). */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useReveals(): RevealsValue {
  return useContext(RevealContext) ?? INERT
}

/** What `RevealAnnouncer` renders: the text, alternating a trailing space so an identical message is read again. */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located with its provider
export function revealAnnouncementText(value: Pick<RevealsValue, 'announcement' | 'announcementTick'>): string {
  return value.announcement === '' ? '' : value.announcement + (value.announcementTick % 2 === 0 ? NBSP : '')
}

/**
 * Whether the reveal sheet is open for the document the canvas shows (Critic 14): the Workbench is
 * active, the sheet names the open canvas document, and that document is open. The shell makes
 * everything else inert only while this holds, so a sheet left over from a document that has since
 * changed or closed can never strand the shell inert.
 */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useRevealSheetOpen(): boolean {
  const { sheet } = useReveals()
  const { doc } = useCanvasState()
  const workbench = useWorkbenchActive()
  return workbench && sheet !== null && doc.kind === 'open' && doc.document.document_id === sheet.documentId
}
