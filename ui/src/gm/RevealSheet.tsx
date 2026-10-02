/**
 * RevealSheet (agent-forge-harness-1kg.7.3, brief 6) -- the GM's staged reveal.
 *
 * Props-driven: it fetches nothing and decides nothing about what may reach a player.
 * `RevealSheetHost` derives the rows (`revealFields`), the draft, the audience and the
 * effect, and passes them in. The GM picks fields and an audience, then Confirms (one
 * staged mutation) or Stops (immediate). Nothing reaches a player without a Confirm.
 *
 * - A modal `role="dialog"` named by its heading. Focus goes to the heading on open, so
 *   the title is read first; Tab wraps inside; on close focus goes back where the host
 *   says (`restoreFocus`). Escape and the scrim are Cancel, and both are inert while a
 *   Confirm is in flight (REVEAL-5, AE-50). Escape is always `preventDefault`ed, so the
 *   shell's document-level drawer Escape never acts behind it.
 * - While a Confirm is in flight the effect button is `aria-disabled`, not natively
 *   `disabled`, so focus stays on it inside the modal; Stop showing is the one control
 *   that stays enabled (X-3).
 * - Field text is rendered as React text nodes only, in a `pre-wrap` block per ticked
 *   row: never interpreted. Every string is in `revealCopy.ts`.
 * - Two native fieldsets, not a radiogroup that owns checkboxes: Who sees it (two
 *   radios) and Players (one checkbox per confirmed seat, disabled unless Chosen
 *   players is chosen).
 */

import * as React from 'react'
import { Switch } from '../ds/Switch'
import { FOCUSABLE_SELECTOR, wrapTab } from '../shell/focusTrap'
import type { Seat } from './contracts'
import { REVEAL_COPY } from './revealCopy'
import { tickedKeys, type DraftAudience, type RevealEffect, type RevealRow } from './revealFields'
import './RevealSheet.css'

export type RevealSheetPhase =
  | 'preparing'
  | 'no_session'
  | 'unknown'
  | 'seal_failed'
  | 'refused'
  | 'unavailable'
  | 'ready'

export interface RevealSheetStart {
  /** A Start is in flight. */
  readonly pending: boolean
  /** What the last Start answered, or `null`. */
  readonly notice: string | null
  /** The last Start can be tried again. */
  readonly retry: boolean
}

export interface RevealSheetSeats {
  readonly status: 'loading' | 'ready' | 'failed'
  /** The seats the picker offers (confirmed only, Critic 2). */
  readonly items: readonly Seat[]
}

export interface RevealSheetProps {
  /** The document's title: GM-private, rendered and nothing more. */
  readonly title: string
  readonly phase: RevealSheetPhase
  /** The visible status line (ready), e.g. the live summary or `Choices reset for Brann`. */
  readonly statusLine: string
  /** The sheet's own status node: mounted empty, filled for `Choices reset for …` and `Reveal changed …`. */
  readonly liveMessage: string
  readonly start: RevealSheetStart
  readonly rows: readonly RevealRow[]
  readonly draft: ReadonlySet<string>
  readonly audience: DraftAudience
  readonly seats: RevealSheetSeats
  readonly effect: RevealEffect
  /** The document is live now: Stop showing is offered. */
  readonly live: boolean
  readonly partialAudience: boolean
  readonly staleNote: boolean
  readonly emptyDocument: boolean
  readonly conflict: boolean
  /** An error line already worded, or `null`. */
  readonly error: string | null
  /** The last Confirm failed and the same one can be sent again. */
  readonly tryAgain: boolean
  readonly stopWaiting: boolean
  readonly confirming: boolean
  onStart(): void
  onToggleRow(row: RevealRow, on: boolean): void
  onChooseTable(): void
  onChoosePlayers(): void
  onToggleSeat(participantId: string, on: boolean): void
  onRetrySeats(): void
  onRetryPrepare(): void
  onConfirm(): void
  onCancel(): void
  onStop(): void
  /** Called once when the sheet goes away: focus returns to the opener. */
  restoreFocus(): void
}

function reasonText(row: RevealRow): string | null {
  if (row.reason === 'asset') return REVEAL_COPY.reasonAsset
  if (row.reason === 'empty') return REVEAL_COPY.reasonEmpty
  return null
}

export function RevealSheet(props: RevealSheetProps): React.JSX.Element {
  const { phase, confirming, title } = props
  const id = React.useId()
  const headingId = `${id}-heading`
  const dialogRef = React.useRef<HTMLDivElement>(null)
  const headingRef = React.useRef<HTMLHeadingElement>(null)
  const restoreRef = React.useRef(props.restoreFocus)
  React.useLayoutEffect(() => {
    restoreRef.current = props.restoreFocus
  })

  React.useEffect(() => {
    // The heading takes focus, so the dialog's name is the first thing announced (a11y).
    headingRef.current?.focus()
    return () => restoreRef.current()
  }, [])

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      // Always stopped here, even while a Confirm is in flight, so the shell's drawer Escape never acts behind us.
      event.preventDefault()
      if (!confirming) props.onCancel()
      return
    }
    const dialog = dialogRef.current
    if (dialog === null) return
    // The heading holds focus on open, so it is the start of the loop: Shift+Tab from it goes to the last control.
    if (event.key === 'Tab' && event.shiftKey && document.activeElement === headingRef.current) {
      const last = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].at(-1)
      if (last !== undefined) {
        event.preventDefault()
        last.focus()
      }
      return
    }
    wrapTab(event, dialog)
  }

  const heading = phase === 'no_session' ? REVEAL_COPY.noSessionHeading : REVEAL_COPY.heading(title)

  return (
    <div
      className="gm-reveal__scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !confirming) props.onCancel()
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className="gm-reveal"
        data-phase={phase}
        onKeyDown={handleKeyDown}
      >
        <h2 id={headingId} ref={headingRef} className="gm-reveal__heading" tabIndex={-1}>
          {heading}
        </h2>
        {/* Mounted empty with the dialog, filled later: a live region that appears with its text is not reliably announced. */}
        <p role="status" className="gm-reveal__sr">
          {props.liveMessage}
        </p>

        {phase === 'preparing' && <Preparing />}
        {phase === 'no_session' && <NoSession {...props} />}
        {phase === 'unknown' && <p className="gm-reveal__body">{REVEAL_COPY.unknown}</p>}
        {phase === 'seal_failed' && (
          <>
            <p className="gm-reveal__body">{REVEAL_COPY.sealFailed}</p>
            <button type="button" className="gm-reveal__button" disabled={confirming} onClick={props.onRetryPrepare}>
              {REVEAL_COPY.tryAgain}
            </button>
          </>
        )}
        {phase === 'refused' && <p className="gm-reveal__body">{REVEAL_COPY.documentRefused}</p>}
        {phase === 'unavailable' && <p className="gm-reveal__body">{REVEAL_COPY.documentUnavailable}</p>}
        {phase === 'ready' && <Ready {...props} idPrefix={id} />}

        <div className="gm-reveal__actions">
          <button type="button" className="gm-reveal__button" disabled={confirming} onClick={props.onCancel}>
            {REVEAL_COPY.cancel}
          </button>
          <span className="gm-reveal__spacer" />
          {phase === 'ready' && props.effect.kind !== 'stop' && <EffectButton {...props} />}
          {(props.live || confirming || phase === 'unknown') && (
            <button type="button" className="gm-reveal__button gm-reveal__button--stop" onClick={props.onStop}>
              {REVEAL_COPY.stopShowing}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/** Skeleton rows and visible text (not a live region): the document is being sealed and the seats read. */
function Preparing(): React.JSX.Element {
  return (
    <div className="gm-reveal__preparing">
      <div className="gm-reveal__skeleton" aria-hidden="true">
        <span className="gm-reveal__bar" />
        <span className="gm-reveal__bar" />
        <span className="gm-reveal__bar" />
      </div>
      <p className="gm-reveal__body">{REVEAL_COPY.preparing}</p>
    </div>
  )
}

function NoSession({ start, onStart }: RevealSheetProps): React.JSX.Element {
  return (
    <>
      <p className="gm-reveal__body">{REVEAL_COPY.noSessionBody}</p>
      {start.notice !== null && (
        <p className="gm-reveal__notice" role="alert">
          {start.notice}
        </p>
      )}
      <button type="button" className="gm-reveal__button gm-reveal__button--primary" disabled={start.pending} onClick={onStart}>
        {start.pending ? REVEAL_COPY.starting : start.retry ? REVEAL_COPY.retry : REVEAL_COPY.startSession}
      </button>
    </>
  )
}

function EffectButton(props: RevealSheetProps): React.JSX.Element {
  const { effect, confirming, tryAgain, stopWaiting, emptyDocument } = props
  const label = confirming ? REVEAL_COPY.revealing : tryAgain ? REVEAL_COPY.tryAgain : effect.label
  const idle = effect.kind === 'none' || stopWaiting || emptyDocument
  return (
    <button
      type="button"
      className="gm-reveal__button gm-reveal__button--primary"
      // In flight it is aria-disabled and not `disabled`, so focus stays on it inside the modal (Critic 13).
      disabled={idle && !confirming}
      aria-disabled={confirming || undefined}
      onClick={() => {
        if (!confirming && !idle) props.onConfirm()
      }}
    >
      {label}
    </button>
  )
}

function Ready(props: RevealSheetProps & { idPrefix: string }): React.JSX.Element {
  const { audience, seats, confirming, idPrefix } = props
  const players = audience.kind === 'participants'
  const chosen = audience.kind === 'participants' ? audience.ids : []
  const notices = [
    ...props.effect.notices,
    ...(props.partialAudience ? [REVEAL_COPY.partialAudience] : []),
    ...(props.staleNote ? [REVEAL_COPY.staleNote] : []),
    ...(props.stopWaiting ? [REVEAL_COPY.waitingForStop] : []),
    ...(props.conflict ? [REVEAL_COPY.conflict] : []),
  ]
  return (
    <>
      <p className="gm-reveal__statusline">{props.statusLine}</p>

      <fieldset className="gm-reveal__group" disabled={confirming}>
        <legend className="gm-reveal__legend">{REVEAL_COPY.whoSees}</legend>
        <label className="gm-reveal__choice">
          <input type="radio" name={`${idPrefix}-audience`} checked={!players} onChange={props.onChooseTable} />
          <span>{REVEAL_COPY.wholeTable}</span>
        </label>
        <label className="gm-reveal__choice">
          <input type="radio" name={`${idPrefix}-audience`} checked={players} onChange={props.onChoosePlayers} />
          <span>{REVEAL_COPY.chosenPlayers}</span>
        </label>
      </fieldset>

      <fieldset className="gm-reveal__group" disabled={!players || confirming}>
        <legend className="gm-reveal__legend">{REVEAL_COPY.playersLegend}</legend>
        {seats.status === 'loading' && <p className="gm-reveal__hint">{REVEAL_COPY.loadingPlayers}</p>}
        {seats.status === 'failed' && <p className="gm-reveal__hint">{REVEAL_COPY.playersFailed}</p>}
        {seats.status === 'ready' && seats.items.length === 0 && <p className="gm-reveal__hint">{REVEAL_COPY.noPlayers}</p>}
        {seats.status === 'ready' &&
          seats.items.map((seat) => (
            <label key={seat.participant_id} className="gm-reveal__choice">
              <input
                type="checkbox"
                checked={chosen.includes(seat.participant_id)}
                disabled={!players || confirming}
                onChange={(event) => props.onToggleSeat(seat.participant_id, event.target.checked)}
              />
              <span>{seat.alias}</span>
            </label>
          ))}
      </fieldset>
      {seats.status === 'failed' && (
        <button type="button" className="gm-reveal__button" disabled={confirming} onClick={props.onRetrySeats}>
          {REVEAL_COPY.retry}
        </button>
      )}

      <div className="gm-reveal__fields" role="group" aria-label={REVEAL_COPY.fieldsLegend}>
        {props.emptyDocument && <p className="gm-reveal__notice">{REVEAL_COPY.emptyDocument}</p>}
        <ul className="gm-reveal__rows">
          {props.rows.map((row) => (
            <FieldRow key={row.id} row={row} draft={props.draft} locked={confirming} idPrefix={idPrefix} onToggle={props.onToggleRow} />
          ))}
        </ul>
      </div>

      {notices.map((notice) => (
        <p key={notice} className="gm-reveal__notice">
          {notice}
        </p>
      ))}
      {props.error !== null && (
        <p className="gm-reveal__notice" data-tone="error" role="alert">
          {props.error}
        </p>
      )}
    </>
  )
}

interface FieldRowProps {
  row: RevealRow
  draft: ReadonlySet<string>
  locked: boolean
  idPrefix: string
  onToggle(row: RevealRow, on: boolean): void
}

function FieldRow({ row, draft, locked, idPrefix, onToggle }: FieldRowProps): React.JSX.Element {
  const ticked = tickedKeys(row, draft)
  const on = ticked.length > 0
  const reason = reasonText(row)
  // The warning and the disabled reason describe the switch. The preview does not: a prose field can run to
  // thousands of characters and would be read again on every focus (INFERRED I-17).
  const describedBy = [
    ...row.warnings.map((_, index) => `${idPrefix}-${row.id}-warn-${index}`),
    ...(reason === null ? [] : [`${idPrefix}-${row.id}-reason`]),
  ]
  const shown = row.preview.filter((entry) => draft.has(entry.key))
  return (
    <li className="gm-reveal__row" data-ticked={on || undefined} data-disabled={!row.selectable || undefined}>
      <div className="gm-reveal__row-head">
        <Switch
          checked={on}
          disabled={!row.selectable || locked}
          ariaLabel={row.label}
          ariaDescribedBy={describedBy.length === 0 ? undefined : describedBy.join(' ')}
          onChange={(next) => onToggle(row, next)}
        />
        <span className="gm-reveal__row-label">{row.label}</span>
      </div>
      {row.warnings.map((warning, index) => (
        <span key={warning} id={`${idPrefix}-${row.id}-warn-${index}`} className="gm-reveal__warning">
          {warning}
        </span>
      ))}
      {reason !== null && (
        <span id={`${idPrefix}-${row.id}-reason`} className="gm-reveal__reason">
          {reason}
        </span>
      )}
      {on &&
        shown.map((entry) => (
          <div key={entry.key} className="gm-reveal__preview">
            {row.preview.length > 1 && <span className="gm-reveal__preview-label">{entry.label}</span>}
            <span className="gm-reveal__preview-text">{entry.text}</span>
          </div>
        ))}
    </li>
  )
}
