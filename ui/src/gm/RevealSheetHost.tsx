/**
 * RevealSheetHost (agent-forge-harness-1kg.7.3, brief 3 and 6, and the Critic's items
 * 4, 5, 6, 7, 8, 9, 10, 13 and 14) -- loads what the sheet needs and wires it to the store.
 *
 * Rendered once, at the shell root. It shows the sheet only while the reveal store's `sheet`
 * names the document the canvas has open (`useRevealSheetOpen`), and closes a sheet that has
 * been left over from a document that changed or closed.
 *
 * Preparing: a hidden document is SEALED, and its answer's `data` and version number are the
 * preview source and the Confirm's version (CANVAS-34). A live document is not sealed: its
 * pinned version is read, and the preview is that text, so the GM sees what the table sees
 * (REVEAL-8). The type comes from that snapshot, and a type this bundle does not know exactly
 * (unknown, a newer `type_version`, or another type than the live one) yields no rows and no
 * Confirm (ED-24, X-8). The seats and, for an owner-audience type, the character sheet's link
 * are read beside it.
 *
 * The draft is the GM's and is never written anywhere (no storage, no URL). It is seeded per
 * REVEAL-4 and re-seeded when the audience changes. The Confirm's session and epoch come from
 * the picture the sheet displays, never from the table session. Success is what the answered
 * picture shows, not the HTTP status. A 409, a refused audience, version or mask, or a replay
 * that came back not live is REVEAL-15: re-read, keep the draft, ask the GM to confirm again,
 * never resend by itself. A Stop is sent at once and closes the sheet (I-14).
 */

import * as React from 'react'
import { useCanvasActions, useCanvasState } from '../shell/canvasContext'
import { listSeats } from '../shell/campaignApi'
import { useReveals, useRevealSheetOpen } from '../shell/revealContext'
import { mintCommandId } from '../shell/tableSessionApi'
import { useTableSession } from '../shell/tableSession'
import type { CharacterSheetLink, Document, RevealRequest, Seat } from './contracts'
import {
  getCharacterSheetLink,
  getDocumentVersion,
  sealDocument,
  type DocumentReadResult,
  type VersionReadResult,
} from './documentApi'
import { documentTitle } from './documentTitle'
import { documentTypeById, seedsOwnerDefault, type DocumentType } from './registry'
import { REVEAL_COPY } from './revealCopy'
import {
  aliasList,
  defaultAudience,
  liveOf,
  maskFromDraft,
  revealEffect,
  revealRows,
  revealSummary,
  seedDraft,
  selectableSeats,
  type DraftAudience,
  type LiveDocument,
} from './revealFields'
import { RevealSheet, type RevealSheetPhase } from './RevealSheet'
import { returnFocus } from './returnFocus'

const NBSP = ' '

// ── Preparation ──────────────────────────────────────────────────────────────

/** The one object the preview text and the Confirm's version both come from (Critic 8). */
interface Snapshot {
  readonly type: string
  readonly typeVersion: number
  readonly data: Readonly<Record<string, unknown>>
  readonly version: number
  readonly sealed: boolean
  readonly archived: boolean
}

type Prepared =
  | { readonly status: 'ready'; readonly snapshot: Snapshot; readonly type: DocumentType; readonly link: CharacterSheetLink | null }
  | { readonly status: 'seal_failed' | 'refused' | 'unavailable' | 'signed_out' }

type SnapshotRead =
  | { readonly kind: 'ok'; readonly snapshot: Snapshot }
  | { readonly kind: 'refused' | 'unavailable' | 'failed' | 'unauthorized' }

function snapshotOfDocument(result: DocumentReadResult): SnapshotRead {
  if (result.kind === 'unsupported') return { kind: 'refused' }
  if (result.kind !== 'ok') return { kind: result.kind }
  const { document } = result
  return {
    kind: 'ok',
    snapshot: {
      type: document.type,
      typeVersion: document.type_version,
      data: document.data,
      version: document.version.number,
      sealed: document.version.sealed,
      archived: document.archived,
    },
  }
}

function snapshotOfVersion(result: VersionReadResult): SnapshotRead {
  if (result.kind === 'unsupported') return { kind: 'refused' }
  if (result.kind !== 'ok') return { kind: result.kind }
  const { snapshot } = result
  return {
    kind: 'ok',
    snapshot: {
      type: snapshot.type,
      typeVersion: snapshot.type_version,
      data: snapshot.data,
      version: snapshot.version.number,
      sealed: snapshot.version.sealed,
      archived: false,
    },
  }
}

interface PrepareArgs {
  readonly campaignId: string
  readonly documentId: string
  /** The live entry's version and type, or `null` for a hidden document. */
  readonly live: Pick<LiveDocument, 'version' | 'type'> | null
  readonly needsLink: boolean
  readonly fetchImpl: typeof fetch
}

async function prepareSheet({ campaignId, documentId, live, needsLink, fetchImpl }: PrepareArgs): Promise<Prepared> {
  const source =
    live === null
      ? sealDocument(campaignId, documentId, fetchImpl).then(snapshotOfDocument)
      : getDocumentVersion(campaignId, documentId, live.version, fetchImpl).then(snapshotOfVersion)
  const [read, link] = await Promise.all([
    source,
    needsLink ? getCharacterSheetLink(campaignId, documentId, fetchImpl) : Promise.resolve(null),
  ])
  if (read.kind === 'unauthorized') return { status: 'signed_out' }
  if (read.kind === 'failed') return { status: 'seal_failed' }
  if (read.kind !== 'ok') return { status: read.kind }
  const { snapshot } = read
  const type = documentTypeById(snapshot.type)
  // Critic 10: a type this bundle does not know exactly is a guess at an allowlist, so no rows and no Confirm.
  if (
    type === undefined ||
    snapshot.typeVersion !== type.type_version ||
    (live !== null && live.type !== snapshot.type) ||
    !snapshot.sealed ||
    snapshot.archived
  ) {
    return { status: 'refused' }
  }
  return { status: 'ready', snapshot, type, link: link !== null && link.kind === 'ok' ? link.link : null }
}

// ── The host ─────────────────────────────────────────────────────────────────

export interface RevealSheetHostProps {
  /** For tests: the `fetch` every call goes through. */
  readonly fetchImpl?: typeof fetch
}

export function RevealSheetHost({ fetchImpl }: RevealSheetHostProps): React.JSX.Element | null {
  const reveals = useReveals()
  const open = useRevealSheetOpen()
  const { doc } = useCanvasState()
  const { closeSheet } = reveals
  // Critic 14: a sheet left over from a document that changed or closed, or a Workbench that went
  // inactive, is closed. `useRevealSheetOpen` is false for it, so the shell is never inert for it.
  const stale = reveals.sheet !== null && !open
  React.useLayoutEffect(() => {
    if (stale) closeSheet()
  }, [stale, closeSheet])
  if (!open || doc.kind !== 'open') return null
  return <OpenSheet key={doc.document.document_id} document={doc.document} fetchImpl={fetchImpl ?? fetch} />
}

interface SeatState {
  readonly status: 'loading' | 'ready' | 'failed'
  readonly items: readonly Seat[]
}

interface Choice {
  readonly audience: DraftAudience
  readonly draft: ReadonlySet<string>
}

interface OpenSheetProps {
  readonly document: Document
  readonly fetchImpl: typeof fetch
}

/** `Shown to the table`, `Shown to Brann`, `Updated what the table sees`, `Moved to Brann`. */
function outcomeText(kind: 'reveal' | 'replace' | 'update' | 'move', audience: DraftAudience, seats: readonly Seat[]): string {
  const who = audience.kind === 'table' ? REVEAL_COPY.tableName : aliasList(audience.ids, seats)
  if (kind === 'update') return REVEAL_COPY.updated(who)
  if (kind === 'move') return REVEAL_COPY.movedTo(who)
  return REVEAL_COPY.shownTo(who)
}

function OpenSheet({ document, fetchImpl }: OpenSheetProps): React.JSX.Element {
  const reveals = useReveals()
  const table = useTableSession()
  const { titleRef } = useCanvasActions()
  const campaignId = document.campaign_id
  const documentId = document.document_id
  const title = documentTitle(document)
  const picture = reveals.state
  const live = liveOf(picture, documentId)
  const declared = documentTypeById(document.type)
  const needsLink = declared !== undefined && seedsOwnerDefault(declared)
  const pictureKey = picture === null ? null : `${picture.session_id}:${picture.reveal_epoch}`

  const [prepared, setPrepared] = React.useState<Prepared | null>(null)
  const [generation, setGeneration] = React.useState(0)
  const [seats, setSeats] = React.useState<SeatState>({ status: 'loading', items: [] })
  const [choice, setChoice] = React.useState<Choice | null>(null)
  const [resetLine, setResetLine] = React.useState<string | null>(null)
  const [liveText, setLiveText] = React.useState('')
  const [liveTick, setLiveTick] = React.useState(0)
  const [conflict, setConflict] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [tryAgain, setTryAgain] = React.useState(false)
  const [confirming, setConfirming] = React.useState(false)
  const [startNotice, setStartNotice] = React.useState<{ text: string; retry: boolean } | null>(null)
  const commandRef = React.useRef<string | null>(null)
  const mountedRef = React.useRef(true)
  React.useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const say = React.useCallback((text: string): void => {
    setLiveText(text)
    setLiveTick((tick) => tick + 1)
  }, [])

  // Critic 8: if the picture's session or epoch moves under an open sheet (another tab, a re-read), the sheet
  // adopts it, keeps the draft and asks the GM to confirm again. Adjusted during render, never by an effect.
  const [seenKey, setSeenKey] = React.useState(pictureKey)
  if (pictureKey !== seenKey) {
    setSeenKey(pictureKey)
    if (seenKey !== null && pictureKey !== null && !confirming) {
      setConflict(true)
      say(REVEAL_COPY.conflict)
    }
  }

  // The seats: read on open, and again on Retry and after a refused audience.
  const noteSeatsRef = React.useRef(reveals.noteSeats)
  React.useLayoutEffect(() => {
    noteSeatsRef.current = reveals.noteSeats
  })
  const [seatGeneration, setSeatGeneration] = React.useState(0)
  const [seatsFor, setSeatsFor] = React.useState(seatGeneration)
  if (seatsFor !== seatGeneration) {
    setSeatsFor(seatGeneration)
    setSeats((current) => ({ status: 'loading', items: current.items }))
  }
  React.useEffect(() => {
    let stale = false
    void listSeats(campaignId, fetchImpl).then((result) => {
      if (stale) return
      if (result.kind === 'ok') {
        setSeats({ status: 'ready', items: result.items })
        noteSeatsRef.current(result.items)
      } else {
        setSeats((current) => ({ status: result.kind === 'unauthorized' ? 'loading' : 'failed', items: current.items }))
      }
    })
    return () => {
      stale = true
    }
  }, [campaignId, fetchImpl, seatGeneration])
  const loadSeats = (): void => setSeatGeneration((value) => value + 1)

  // The preparation, keyed by what it depends on: the document's live version (or hidden), and a retry counter.
  const readyForPrep = reveals.status === 'live'
  const requestKey = `${generation}:${live === null ? 'hidden' : `live${live.version}`}:${readyForPrep}`
  const [preparedFor, setPreparedFor] = React.useState(requestKey)
  if (preparedFor !== requestKey) {
    setPreparedFor(requestKey)
    setPrepared(null)
  }
  const liveVersion = live === null ? null : live.version
  const liveType = live === null ? null : live.type
  React.useEffect(() => {
    if (!readyForPrep) return undefined
    let stale = false
    const pinned = liveVersion === null || liveType === null ? null : { version: liveVersion, type: liveType }
    void prepareSheet({ campaignId, documentId, live: pinned, needsLink, fetchImpl }).then((result) => {
      if (!stale) setPrepared(result)
    })
    return () => {
      stale = true
    }
  }, [readyForPrep, generation, liveVersion, liveType, campaignId, documentId, needsLink, fetchImpl])

  const rows = React.useMemo(
    () => (prepared?.status === 'ready' ? revealRows(prepared.type, prepared.snapshot.data) : []),
    [prepared],
  )
  const offered = React.useMemo(() => selectableSeats(seats.items), [seats.items])
  const seatsSettled = seats.status !== 'loading'

  // The first seeding, once the document and the seats have both answered (REVEAL-4, Critic 5, 6).
  if (prepared?.status === 'ready' && choice === null && seatsSettled) {
    const first = defaultAudience(prepared.type, picture, documentId, prepared.link, seats.items)
    setChoice({ audience: first.audience, draft: seedDraft(prepared.type, rows, first.audience, live, prepared.link, seats.items) })
  }

  const phase = phaseOf({ reveals: reveals.status, prepared, hasChoice: choice !== null })

  // What the sheet displays is the draft as it stands now: keys a re-read took away are dropped, and so are
  // removed or unconfirmed seats (Critic 7). The stored draft is never trusted for what is sent.
  const audience: DraftAudience = React.useMemo(() => {
    if (choice === null || choice.audience.kind === 'table') return choice?.audience ?? { kind: 'table' }
    const known = new Set(offered.map((seat) => seat.participant_id))
    return seats.status === 'ready' ? { kind: 'participants', ids: choice.audience.ids.filter((id) => known.has(id)) } : choice.audience
  }, [choice, offered, seats.status])
  const mask = prepared?.status === 'ready' && choice !== null ? maskFromDraft(prepared.type, rows, choice.draft) : []
  const draft: ReadonlySet<string> = new Set(mask)
  const effect = revealEffect({ picture, documentId, audience, mask, seats: seats.items })
  const summary = prepared?.status === 'ready' ? revealSummary(picture, documentId, prepared.type, seats.items) : null
  const partial =
    live !== null && live.audience.kind === 'participants' && seats.status === 'ready' && live.audience.ids.some((id) => !offered.some((seat) => seat.participant_id === id))
  const statusLine =
    resetLine ?? (summary === null ? REVEAL_COPY.statusHidden : REVEAL_COPY.statusLive(summary.fields, summary.audience))

  // ── Choices ──
  const reseed = (next: DraftAudience): void => {
    if (prepared?.status !== 'ready') return
    setChoice({ audience: next, draft: seedDraft(prepared.type, rows, next, live, prepared.link, seats.items) })
    setConflict(false)
    if (next.kind === 'table' || next.ids.length > 0) {
      const line = REVEAL_COPY.choicesReset(next.kind === 'table' ? REVEAL_COPY.tableName : aliasList(next.ids, seats.items))
      setResetLine(line)
      say(line)
    }
  }
  const chooseTable = (): void => reseed({ kind: 'table' })
  const choosePlayers = (): void => {
    // Choosing Chosen players empties the draft (Critic 7); the first seat ticked seeds it.
    setChoice({ audience: { kind: 'participants', ids: [] }, draft: new Set() })
    setResetLine(null)
  }
  const toggleSeat = (participantId: string, on: boolean): void => {
    const current = audience.kind === 'participants' ? audience.ids : []
    const ids = on ? [...current.filter((id) => id !== participantId), participantId] : current.filter((id) => id !== participantId)
    if (ids.length === 0) {
      setChoice({ audience: { kind: 'participants', ids: [] }, draft: new Set() })
      setResetLine(null)
    } else {
      reseed({ kind: 'participants', ids })
    }
  }
  const toggleRow = (row: { keys: readonly string[] }, on: boolean): void => {
    if (choice === null) return
    const next = new Set(choice.draft)
    for (const key of row.keys) {
      if (on) next.add(key)
      else next.delete(key)
    }
    setChoice({ ...choice, draft: next })
  }

  // ── Leaving ──
  const restoreFocus = (): void => {
    const opener = reveals.openerRef.current
    returnFocus(opener instanceof HTMLElement ? opener : null, titleRef.current)
  }
  const cancel = (): void => reveals.closeSheet()
  const stop = (): void => {
    // Never gated, queued or confirmed by a dialog (X-3); the sheet closes and the header shows the outcome (I-14).
    reveals.stop(documentId, title)
    reveals.closeSheet()
  }

  // ── Confirm ──
  const rePrepare = (): void => {
    setGeneration((value) => value + 1)
    loadSeats()
  }
  const reread = (): void => {
    setConflict(true)
    setError(null)
    setTryAgain(false)
    say(REVEAL_COPY.conflict)
    commandRef.current = null
    rePrepare()
  }
  const confirm = async (): Promise<void> => {
    if (confirming || prepared?.status !== 'ready' || picture === null || effect.kind === 'none' || effect.kind === 'stop') return
    const wire: RevealRequest['audience'] =
      audience.kind === 'table' ? { kind: 'table' } : { kind: 'participants', participant_ids: [...audience.ids] }
    const kind = effect.kind
    const sent: DraftAudience = audience
    commandRef.current ??= mintCommandId()
    setConfirming(true)
    setError(null)
    setConflict(false)
    const outcome = await reveals.confirm(
      {
        document_id: documentId,
        session_id: picture.session_id,
        reveal_epoch: picture.reveal_epoch,
        version: prepared.snapshot.version,
        mask,
        audience: wire,
      },
      commandRef.current,
    )
    if (!mountedRef.current) return
    setConfirming(false)
    switch (outcome.kind) {
      case 'ok':
        if (outcome.result === 'held') {
          commandRef.current = null
          reveals.announce(outcomeText(kind, sent, seats.items))
          reveals.closeSheet()
        } else if (outcome.result === 'stopped') {
          commandRef.current = null
          reveals.closeSheet()
        } else {
          reread()
        }
        return
      case 'conflict':
      case 'mask_refused':
        reread()
        return
      case 'refused':
        if (outcome.field === 'document_id') {
          setPrepared({ status: 'refused' })
        } else {
          reread()
        }
        return
      case 'unavailable':
        setPrepared({ status: 'unavailable' })
        return
      case 'throttled':
        setError(REVEAL_COPY.throttled(outcome.retryAfterS))
        return
      case 'failed':
        // Try again resends with the SAME command id: the route is replay-safe.
        setError(REVEAL_COPY.failed)
        setTryAgain(true)
        return
      case 'blocked':
      case 'unauthorized':
        return
    }
  }

  // ── No live session (REVEAL-1) ──
  const start = async (): Promise<void> => {
    const outcome = await table.start()
    if (!mountedRef.current) return
    switch (outcome) {
      case 'started':
      case 'skipped':
      case 'stale':
        setStartNotice(null)
        void reveals.refresh()
        return
      case 'live_elsewhere':
        setStartNotice({ text: REVEAL_COPY.liveElsewhere, retry: false })
        return
      case 'refused':
        setStartNotice({ text: REVEAL_COPY.startRefused, retry: false })
        return
      case 'throttled':
        setStartNotice({ text: REVEAL_COPY.startThrottled, retry: true })
        return
      case 'failed':
      case 'unavailable':
        setStartNotice({ text: REVEAL_COPY.startFailed, retry: true })
        return
      case 'signed_out':
        return
    }
  }

  return (
    <RevealSheet
      title={title}
      phase={phase}
      statusLine={statusLine}
      liveMessage={liveText === '' ? '' : liveText + (liveTick % 2 === 0 ? NBSP : '')}
      start={{ pending: table.pending === 'start', notice: startNotice?.text ?? null, retry: startNotice?.retry === true }}
      rows={rows}
      draft={draft}
      audience={audience}
      seats={{ status: seats.status, items: offered }}
      effect={effect}
      live={live !== null}
      partialAudience={partial}
      staleNote={live?.staleText === true}
      emptyDocument={prepared?.status === 'ready' && !rows.some((row) => row.selectable)}
      conflict={conflict}
      error={error}
      tryAgain={tryAgain}
      stopWaiting={reveals.stopping.size > 0}
      confirming={confirming}
      onStart={() => void start()}
      onToggleRow={toggleRow}
      onChooseTable={chooseTable}
      onChoosePlayers={choosePlayers}
      onToggleSeat={toggleSeat}
      onRetrySeats={loadSeats}
      onRetryPrepare={rePrepare}
      onConfirm={() => void confirm()}
      onCancel={cancel}
      onStop={stop}
      restoreFocus={restoreFocus}
    />
  )
}

function phaseOf(args: {
  reveals: 'idle' | 'loading' | 'none' | 'live' | 'unknown'
  prepared: Prepared | null
  hasChoice: boolean
}): RevealSheetPhase {
  if (args.reveals === 'none') return 'no_session'
  if (args.reveals === 'unknown') return 'unknown'
  if (args.reveals !== 'live' || args.prepared === null) return 'preparing'
  switch (args.prepared.status) {
    case 'seal_failed':
      return 'seal_failed'
    case 'refused':
      return 'refused'
    case 'unavailable':
      return 'unavailable'
    case 'signed_out':
      return 'preparing'
    case 'ready':
      return args.hasChoice ? 'ready' : 'preparing'
  }
}
