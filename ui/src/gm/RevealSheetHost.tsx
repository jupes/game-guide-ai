/**
 * RevealSheetHost (agent-forge-harness-1kg.7.3, brief 3 and 6, and the Critic's items
 * 4, 5, 6, 7, 8, 9, 10, 13 and 14) -- loads what the sheet needs and wires it to the store.
 *
 * Rendered once, at the shell root. It shows the sheet only while `useRevealSheetOpen` holds: the
 * canvas document that was open when the sheet opened is still the open one. The sheet is for the
 * canvas document or, from the workspace indicator (REVEAL-14), for another live document, read by
 * its own id, without swapping the canvas. A sheet left over from a document that changed or
 * closed is closed.
 *
 * REVEAL-8 (PR-2): on a live document whose revealed text moved on, the GM may choose Use latest
 * version. That seals the working text and shows every ticked field with the table's text beside the
 * latest; Confirm then pins the version displayed. Nothing is chosen for the GM, and a re-read of the
 * pin (a 409, a refused version) resets the choice.
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
import { listCampaignSeats } from '../shell/campaignApi'
import { useCampaign } from '../shell/campaignContext'
import { useReveals, useRevealSheetOpen } from '../shell/revealContext'
import { mintCommandId } from '../shell/tableSessionApi'
import { useTableSession } from '../shell/tableSession'
import type { CharacterSheetLink, Document, RevealRequest, Seat } from './contracts'
import { getCharacterSheetLink, getDocument, getDocumentVersion, sealDocument } from './documentApi'
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
  type RevealRow,
} from './revealFields'
import { snapshotOfDocument, snapshotOfVersion, snapshotUsable, type Snapshot } from './revealSnapshot'
import { RevealSheet, type LatestReviewRow, type RevealSheetLatest, type RevealSheetPhase } from './RevealSheet'
import { returnFocus } from './returnFocus'

const NBSP = ' '

// ── Preparation ──────────────────────────────────────────────────────────────

type Prepared =
  | { readonly status: 'ready'; readonly snapshot: Snapshot; readonly type: DocumentType; readonly link: CharacterSheetLink | null }
  | { readonly status: 'seal_failed' | 'refused' | 'unavailable' | 'signed_out' }

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
  if (!snapshotUsable(snapshot, type, live)) return { status: 'refused' }
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
  const campaignId = useCampaign().scope?.campaignId ?? null
  const { closeSheet } = reveals
  // Critic 14: a sheet left over from a document that changed or closed, or a Workbench that went
  // inactive, is closed. `useRevealSheetOpen` is false for it, so the shell is never inert for it.
  const stale = reveals.sheet !== null && !open
  React.useLayoutEffect(() => {
    if (stale) closeSheet()
  }, [stale, closeSheet])
  if (!open || reveals.sheet === null) return null
  const { documentId } = reveals.sheet
  // The canvas document's sheet works from the document the canvas already holds.
  if (doc.kind === 'open' && doc.document.document_id === documentId) {
    return <OpenSheet key={documentId} document={doc.document} fetchImpl={fetchImpl ?? fetch} />
  }
  // REVEAL-14: the workspace indicator opens another live document's sheet without swapping the canvas.
  if (campaignId === null) return null
  return <OtherDocumentSheet key={documentId} campaignId={campaignId} documentId={documentId} fetchImpl={fetchImpl ?? fetch} />
}

type OtherRead = { readonly status: 'loading' | 'unavailable' } | { readonly status: 'ok'; readonly document: Document }

const NO_ROWS: readonly RevealRow[] = []
const NO_KEYS: ReadonlySet<string> = new Set()
const noop = (): void => undefined

/**
 * A sheet for a document the canvas does not show (REVEAL-14): the document is read by its own id, then the
 * same `OpenSheet` takes over. Until then, and when it cannot be read, the sheet is a dialog with only Cancel,
 * so the shell is never inert behind nothing.
 */
function OtherDocumentSheet({
  campaignId,
  documentId,
  fetchImpl,
}: {
  readonly campaignId: string
  readonly documentId: string
  readonly fetchImpl: typeof fetch
}): React.JSX.Element {
  const reveals = useReveals()
  const { titleRef } = useCanvasActions()
  const [read, setRead] = React.useState<OtherRead>({ status: 'loading' })
  const closing = React.useRef(false)
  React.useEffect(() => {
    let stale = false
    void getDocument(campaignId, documentId, fetchImpl).then((result) => {
      if (stale) return
      if (result.kind === 'ok') setRead({ status: 'ok', document: result.document })
      else if (result.kind !== 'unauthorized') setRead({ status: 'unavailable' })
    })
    return () => {
      stale = true
    }
  }, [campaignId, documentId, fetchImpl])
  if (read.status === 'ok') return <OpenSheet document={read.document} fetchImpl={fetchImpl} />
  return (
    <RevealSheet
      title={reveals.titles.get(documentId) ?? REVEAL_COPY.aDocument}
      phase={read.status === 'loading' ? 'preparing' : 'unavailable'}
      statusLine=""
      liveMessage=""
      start={{ pending: false, notice: null, retry: false }}
      rows={NO_ROWS}
      draft={NO_KEYS}
      audience={{ kind: 'table' }}
      seats={{ status: 'loading', items: [] }}
      effect={{ kind: 'none', label: REVEAL_COPY.effectNone, notices: [] }}
      live={false}
      partialAudience={false}
      latest={{ status: 'none' }}
      emptyDocument={false}
      conflict={false}
      error={null}
      tryAgain={false}
      stopWaiting={false}
      confirming={false}
      onStart={noop}
      onToggleRow={noop}
      onChooseTable={noop}
      onChoosePlayers={noop}
      onToggleSeat={noop}
      onRetrySeats={noop}
      onRetryPrepare={noop}
      onConfirm={noop}
      onStop={noop}
      onUseLatest={noop}
      onKeepPinned={noop}
      onCancel={() => {
        closing.current = true
        reveals.closeSheet()
      }}
      // Only a real close returns focus: the swap to the loaded sheet must not.
      restoreFocus={() => {
        if (!closing.current) return
        const opener = reveals.openerRef.current
        returnFocus(opener instanceof HTMLElement ? opener : null, titleRef.current)
      }}
    />
  )
}

interface SeatState {
  readonly status: 'loading' | 'ready' | 'failed'
  readonly items: readonly Seat[]
}

interface Choice {
  readonly audience: DraftAudience
  readonly draft: ReadonlySet<string>
}

/** REVEAL-8: the GM chose to review the latest text of a live document whose revealed text moved on. */
type Latest = { readonly status: 'idle' | 'loading' | 'failed' } | { readonly status: 'ready'; readonly snapshot: Snapshot }

/** The ticked fields, each with the text the table has beside the latest. Plain strings, rendered as text. */
function reviewRows(mask: readonly string[], pinned: readonly RevealRow[], latest: readonly RevealRow[]): LatestReviewRow[] {
  const entry = (rows: readonly RevealRow[], key: string) => rows.flatMap((row) => row.preview).find((item) => item.key === key)
  return mask.flatMap((key) => {
    const next = entry(latest, key)
    return next === undefined ? [] : [{ key, label: next.label, oldText: entry(pinned, key)?.text ?? '', newText: next.text }]
  })
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
  const [latest, setLatest] = React.useState<Latest>({ status: 'idle' })
  const [startNotice, setStartNotice] = React.useState<{ text: string; retry: boolean } | null>(null)
  const commandRef = React.useRef<string | null>(null)
  const requestKeyRef = React.useRef('')
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
    void listCampaignSeats(campaignId, fetchImpl).then((result) => {
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
    setLatest({ status: 'idle' })
  }
  React.useLayoutEffect(() => {
    requestKeyRef.current = requestKey
  })
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

  // The one snapshot the preview and the Confirm's version come from: the pinned one, or the latest the GM chose to review (REVEAL-8).
  const usingLatest = latest.status === 'ready' && live !== null && live.staleText
  const source: Snapshot | null =
    prepared?.status === 'ready' ? (usingLatest && latest.status === 'ready' ? latest.snapshot : prepared.snapshot) : null
  const rows = React.useMemo(
    () => (prepared?.status === 'ready' && source !== null ? revealRows(prepared.type, source.data) : []),
    [prepared, source],
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
  const effect = revealEffect({
    picture,
    documentId,
    audience,
    mask,
    seats: seats.items,
    versionChanged: usingLatest && live !== null && source !== null && source.version !== live.version,
  })
  const pinnedRows = prepared?.status === 'ready' && usingLatest ? revealRows(prepared.type, prepared.snapshot.data) : []
  const latestProp: RevealSheetLatest =
    prepared?.status !== 'ready' || live === null || !live.staleText
      ? { status: 'none' }
      : usingLatest
        ? { status: 'review', rows: reviewRows(mask, pinnedRows, rows) }
        : { status: latest.status === 'failed' || latest.status === 'loading' ? latest.status : 'offer' }
  const summary = prepared?.status === 'ready' ? revealSummary(picture, documentId, prepared.type, seats.items) : null
  const partial =
    live !== null && live.audience.kind === 'participants' && seats.status === 'ready' && live.audience.ids.some((id) => !offered.some((seat) => seat.participant_id === id))
  const statusLine =
    resetLine ?? (summary === null ? REVEAL_COPY.statusHidden : REVEAL_COPY.statusLive(summary.fields, summary.audience))

  // ── Choices ──
  // F-7: a failure belongs to the choice that failed. Any edit is a different intent, so it drops the failure's line and its
  // Try again label, and the next press mints a new command id (Try again with the SAME choice still reuses it).
  const edited = (): void => {
    commandRef.current = null
    setError(null)
    setTryAgain(false)
  }
  const reseed = (next: DraftAudience): void => {
    if (prepared?.status !== 'ready') return
    edited()
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
    edited()
    setChoice({ audience: { kind: 'participants', ids: [] }, draft: new Set() })
    setResetLine(null)
  }
  const toggleSeat = (participantId: string, on: boolean): void => {
    const current = audience.kind === 'participants' ? audience.ids : []
    const ids = on ? [...current.filter((id) => id !== participantId), participantId] : current.filter((id) => id !== participantId)
    if (ids.length === 0) {
      edited()
      setChoice({ audience: { kind: 'participants', ids: [] }, draft: new Set() })
      setResetLine(null)
    } else {
      reseed({ kind: 'participants', ids })
    }
  }
  const toggleRow = (row: { keys: readonly string[] }, on: boolean): void => {
    if (choice === null) return
    edited()
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

  // ── REVEAL-8: Use latest version ──
  const loadLatestVersion = async (): Promise<void> => {
    if (prepared?.status !== 'ready' || latest.status === 'loading' || confirming) return
    const startedAt = requestKeyRef.current
    edited()
    setLatest({ status: 'loading' })
    const read = snapshotOfDocument(await sealDocument(campaignId, documentId, fetchImpl))
    // The pin moved, or the sheet went away, while this was in flight: it answers for a preparation that is gone.
    if (!mountedRef.current || requestKeyRef.current !== startedAt) return
    if (read.kind === 'unauthorized') {
      setLatest({ status: 'idle' })
      return
    }
    const usable = read.kind === 'ok' && snapshotUsable(read.snapshot, documentTypeById(read.snapshot.type), live)
    setLatest(read.kind === 'ok' && usable ? { status: 'ready', snapshot: read.snapshot } : { status: 'failed' })
  }
  const keepPinned = (): void => {
    edited()
    setLatest({ status: 'idle' })
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
    if (confirming || prepared?.status !== 'ready' || source === null || picture === null || effect.kind === 'none' || effect.kind === 'stop') return
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
        version: source.version,
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
      latest={latestProp}
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
      onUseLatest={() => void loadLatestVersion()}
      onKeepPinned={keepPinned}
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
