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
 * No URL and no realtime channel (1kg.7.5): the picture is re-read on scope entry, when the
 * table session's id or epoch moves, when the sheet opens, after a refusal, when the GM comes
 * back to the tab or window (focus, visibility), and on a bounded backoff while the read keeps
 * failing. The one thing stored is REVEAL-16's opaque pending-stop marker (ids and an epoch,
 * `revealStopMarker.ts`): written when a Stop is pressed, cleared when the server answers, and
 * replayed once on the next load into the same session and epoch only. An unacknowledged Stop
 * blocks unload, and a 401 while something is live leaves what the table can still see for the
 * Login screen (`revealSignOut.ts`). The titles of the live documents are held here too, for the
 * workspace indicator (REVEAL-14); they are GM-private text, rendered and never stored.
 */

import * as React from 'react'
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'
import { addUnauthorizedListener } from '../api'
import type { RevealRequest, RevealState, Seat } from '../gm/contracts'
import { getDocument } from '../gm/documentApi'
import { documentTitle } from '../gm/documentTitle'
import { REVEAL_COPY } from '../gm/revealCopy'
import { confirmReveal, readReveals, type ConfirmResult } from '../gm/revealApi'
import { liveOf, sameAudience, type DraftAudience } from '../gm/revealFields'
import { listCampaignSeats } from './campaignApi'
import { useCampaign, type CampaignScope } from './campaignContext'
import { useCanvasState, useWorkbenchActive } from './canvasContext'
import { CurrentUserContext } from './currentUser'
import { STOP_ALL, StopCourier } from './revealStop'
import { clearPendingStop, readPendingStops, replayable, writePendingStop } from './revealStopMarker'
import { revealSignOutNotice } from './revealSignOut'
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
  /** The canvas document that was open when the sheet opened (`null`: none). The sheet closes when that changes. */
  readonly sheetFrom: string | null
  /** Titles of the live documents, by id: GM-private, for the workspace indicator and the sign-out notice. */
  readonly titles: ReadonlyMap<string, string>
  /** What had focus when the sheet was opened, for the return of focus on close. Stable for the provider's life. */
  readonly openerRef: React.RefObject<Element | null>
  /** The seats known so far, or `null` until read. The header names a private audience with them. */
  readonly seats: readonly Seat[] | null
  /** `''` until an outcome. */
  readonly announcement: string
  /** Changes with every announcement, so an identical message is announced again. */
  readonly announcementTick: number
  /** `opener` is whatever had focus at the gesture. `canvasDocumentId` is the canvas document open now (default: `documentId` itself). */
  openSheet(documentId: string, opener?: Element | null, canvasDocumentId?: string | null): void
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
  readonly sheetFrom: string | null
  readonly titles: ReadonlyMap<string, string>
  readonly seats: readonly Seat[] | null
  readonly announcement: string
  readonly announcementTick: number
  /** Documents whose Stop the server refused outright. Cleared by the next press for the document. */
  readonly refused: ReadonlySet<string>
}

const NONE: ReadonlySet<string> = new Set()

const NO_TITLES: ReadonlyMap<string, string> = new Map()

const EMPTY: Snap = {
  token: null, read: 'loading', state: null, sheet: null, sheetFrom: null, titles: NO_TITLES, seats: null, announcement: '',
  announcementTick: 0, refused: NONE,
}

/** Two reads closer than this are one: a focus and a visibility change arrive together. */
const FOCUS_READ_GAP_MS = 2_000

const liveDocumentIds = (picture: RevealState | null): string[] => [
  ...new Set(picture?.slots.flatMap((entry) => (entry.live === null ? [] : [entry.live.document_id])) ?? []),
]

class RevealStore extends Emitter {
  private snap: Snap = EMPTY
  /** The seats are asked for once per scope on the header's account; the sheet reads them itself when it opens. */
  private seatsAsked = false
  /** The documents whose title has been asked for in this scope; a failed read is forgotten so the next picture asks again. */
  private readonly titlesAsked = new Set<string>()
  /** When the latest read was sent, for the focus dedupe. */
  private lastReadAt = 0
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
    this.titlesAsked.clear()
    this.lastReadAt = 0
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
    this.ensureTitles(token, incoming)
    return true
  }

  /** The workspace indicator names what is live by title: read each live document once (a GM-private read; the id is the only thing in the URL). */
  private ensureTitles(token: string, picture: RevealState | null): void {
    const campaignId = this.campaignId
    if (campaignId === null) return
    for (const documentId of liveDocumentIds(picture)) {
      if (this.titlesAsked.has(documentId)) continue
      this.titlesAsked.add(documentId)
      void getDocument(campaignId, documentId, this.fetchImpl).then((result) => {
        if (result.kind === 'ok') this.noteTitle(token, documentId, documentTitle(result.document))
        else this.titlesAsked.delete(documentId)
      })
    }
  }

  private noteTitle(token: string, documentId: string, title: string): void {
    this.patch(token, { titles: new Map([...this.snap.titles, [documentId, title]]) })
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
    this.lastReadAt = Date.now()
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

  /** The GM came back to the tab: read again, unless a read was only just sent. */
  returned(token: string, campaignId: string, fetchImpl: typeof fetch): void {
    if (this.snap.token !== token || Date.now() - this.lastReadAt < FOCUS_READ_GAP_MS) return
    void this.read(token, campaignId, fetchImpl)
  }

  /** The table session's id or epoch moved: read again unless the held picture already matches. */
  sync(token: string, campaignId: string, live: { sessionId: string; revealEpoch: number } | null, fetchImpl: typeof fetch): void {
    if (this.snap.token !== token) return
    const { state, read } = this.snap
    if (live === null) return
    if (read === 'ready' && state !== null && state.session_id === live.sessionId && state.reveal_epoch >= live.revealEpoch) return
    void this.read(token, campaignId, fetchImpl)
  }

  openSheet(token: string, campaignId: string, documentId: string, from: string | null, fetchImpl: typeof fetch): void {
    this.patch(token, { sheet: { documentId }, sheetFrom: from })
    void this.read(token, campaignId, fetchImpl)
  }

  closeSheet(token: string): void {
    if (this.snap.sheet !== null) this.patch(token, { sheet: null, sheetFrom: null })
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
  sheetFrom: null,
  titles: NO_TITLES,
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

  // Delivers a Stop and settles what follows it. `replayId` is a marker's command id, replayed after a reload (REVEAL-16).
  const deliver = React.useCallback(
    (documentId: string, title: string | undefined, replayId?: string): void => {
      if (token === null || campaignId === null) return
      store.pressed(documentId)
      const seq = store.stopSeq(documentId)
      const held = store.getSnapshot().state
      const done = courier.send(campaignId, documentId, fetchImpl, userId, replayId)
      const commandId = courier.commandIdOf(campaignId, documentId)
      // The marker is written before the first answer, so a reload mid-flight still replays it; ids and an epoch only.
      if (replayId === undefined && userId !== null && commandId !== null && held !== null) {
        writePendingStop(userId, { campaignId, documentId, sessionId: held.session_id, epoch: held.reveal_epoch, commandId })
      }
      const named = title ?? REVEAL_COPY.thisDocument
      void done.then((outcome) => {
        if (outcome.kind !== 'signed_out' && userId !== null) clearPendingStop(userId, campaignId, documentId)
        if (outcome.kind === 'stopped') {
          store.stopped(token, campaignId, seq, outcome.state, fetchImpl)
          store.announce(token, REVEAL_COPY.stopped(named))
        } else if (outcome.kind === 'gone') {
          void store.read(token, campaignId, fetchImpl)
        } else if (outcome.kind === 'invalid') {
          store.stopRefused(token, documentId)
          store.announce(token, REVEAL_COPY.stopInvalid(named))
        }
      })
    },
    [token, campaignId, store, courier, fetchImpl, userId],
  )

  // REVEAL-16: a Stop left unacknowledged by the last load is replayed once, first, but only into the session and
  // epoch it was pressed at; anything else is dropped, so it can never kill a later, deliberate reveal.
  const replayedFor = React.useRef<string | null>(null)
  const pictureReady = current !== null && current.read === 'ready'
  const readPicture = current?.state ?? null
  const readTitles = current?.titles ?? NO_TITLES
  useEffect(() => {
    if (token === null || campaignId === null || userId === null || !pictureReady || replayedFor.current === token) return
    replayedFor.current = token
    for (const stop of readPendingStops(userId)) {
      if (stop.campaignId !== campaignId) continue
      if (!replayable(stop, readPicture)) {
        clearPendingStop(userId, stop.campaignId, stop.documentId)
        continue
      }
      deliver(stop.documentId, stop.documentId === STOP_ALL ? REVEAL_COPY.everything : readTitles.get(stop.documentId), stop.commandId)
    }
  }, [token, campaignId, userId, pictureReady, readPicture, readTitles, deliver])

  // An unacknowledged Stop blocks unload (REVEAL-16): the marker covers a reload the GM confirms anyway.
  const unacknowledged = useMemo(
    () => courier.anyPending(),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the version is the dependency
    [courier, courierVersion],
  )
  useEffect(() => {
    if (!unacknowledged) return undefined
    const guard = (event: Event): void => {
      event.preventDefault()
      ;(event as BeforeUnloadEvent).returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [unacknowledged])

  // Coming back to the tab or the window reads the picture again (the realtime channel is 1kg.7.5's).
  useEffect(() => {
    if (token === null || campaignId === null) return undefined
    const back = (): void => store.returned(token, campaignId, fetchImpl)
    const shown = (): void => {
      if (document.visibilityState === 'visible') back()
    }
    window.addEventListener('focus', back)
    document.addEventListener('visibilitychange', shown)
    return () => {
      window.removeEventListener('focus', back)
      document.removeEventListener('visibilitychange', shown)
    }
  }, [store, token, campaignId, fetchImpl])

  // A 401 while something is live: the Login screen says what the table can still see (REVEAL-16).
  const stillLive = React.useRef<string[]>([])
  React.useLayoutEffect(() => {
    const ids = current === null || current.read !== 'ready' ? [] : liveDocumentIds(current.state)
    stillLive.current = ids.map((id) => current?.titles.get(id) ?? REVEAL_COPY.aDocument)
  })
  useEffect(() => addUnauthorizedListener(() => revealSignOutNotice.set(stillLive.current)), [])

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
      sheetFrom: shown.sheetFrom,
      titles: shown.titles,
      openerRef,
      seats: shown.seats,
      announcement: shown.announcement,
      announcementTick: shown.announcementTick,
      openSheet: (documentId, opener, canvasDocumentId = documentId) => {
        openerRef.current = opener ?? null
        store.openSheet(token, campaignId, documentId, canvasDocumentId, fetchImpl)
      },
      closeSheet: () => store.closeSheet(token),
      noteSeats: (seats) => store.noteSeats(token, seats),
      announce: (text) => store.announce(token, text),
      refresh: () => store.read(token, campaignId, fetchImpl),
      confirm: (request, commandId) => store.confirm(token, campaignId, request, commandId, stopping.size > 0, fetchImpl),
      stop: (documentId, title) => deliver(documentId, title),
    }
  }, [token, campaignId, current, stopping, failing, store, fetchImpl, openerRef, deliver])

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
 * Whether the reveal sheet is open (Critic 14): the Workbench is active, and the canvas document that was
 * open when the sheet opened (`sheetFrom`) is still the one open. The sheet may be for that document or,
 * from the workspace indicator, for another (REVEAL-14); either way a Back, a hashchange or a closed
 * canvas ends it. The shell makes everything else inert only while this holds, so a sheet left over from
 * a document that has since changed or closed can never strand the shell inert.
 */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useRevealSheetOpen(): boolean {
  const { sheet, sheetFrom } = useReveals()
  const { doc } = useCanvasState()
  const workbench = useWorkbenchActive()
  const canvasDocument = doc.kind === 'open' ? doc.document.document_id : null
  return workbench && sheet !== null && canvasDocument === sheetFrom
}
