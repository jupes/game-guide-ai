/**
 * RevealIndicator -- the one workspace indicator that lists every live projection
 * (agent-forge-harness-1kg.7.3 PR-2; REVEAL-14, REVEAL-13, REVEAL-16, REVEAL-8, X-9).
 *
 * It lives in the AppHeader row, so it is on screen in every channel and on every layout. It reads
 * `useReveals()`, the same store the canvas header reads, so chat and canvas agree by construction.
 * `Revealed · Ondrey (table) · +1 more` opens a list of every projection with its title; Stop is always
 * its own button (`Stop all (n)`, and one per row), sent at once and never confirmed by a dialog (X-3).
 *
 * In the GM channel a listed projection opens THAT document's sheet, without swapping the canvas. In
 * Sage, Spell and Rules it is Stop-only: a listed projection takes the GM back to the GM channel
 * instead, and `Update…` is not offered, so no reveal can be widened from outside it (X-9, AE-82).
 *
 * It stays operable above any modal: the shell leaves it out of what it makes `inert`, and its CSS
 * lifts it above the dialogs' scrims. A title is GM-private text (X-7): rendered, never stored.
 */

import * as React from 'react'
import { REVEAL_COPY } from '../gm/revealCopy'
import { projectionSummary, revealProjections, type Projection } from '../gm/revealPresentation'
import { useAppNav } from './AppNav'
import { useCanvasState, useWorkbenchActive } from './canvasContext'
import { useReveals } from './revealContext'
import { STOP_ALL } from './revealStop'
import './RevealIndicator.css'

export interface RevealIndicatorProps {
  /** Where focus goes when the indicator goes away while it holds focus (the last Stop was pressed). */
  readonly fallbackFocus?: () => void
}

export function RevealIndicator({ fallbackFocus }: RevealIndicatorProps): React.JSX.Element | null {
  const reveals = useReveals()
  const projections = React.useMemo(
    () => revealProjections(reveals.state, reveals.titles, reveals.seats ?? []),
    [reveals.state, reveals.titles, reveals.seats],
  )
  const unknown = reveals.status === 'unknown'
  if (!unknown && (reveals.status !== 'live' || projections.length === 0)) return null
  return <IndicatorBody projections={projections} unknown={unknown} fallbackFocus={fallbackFocus} />
}

interface IndicatorBodyProps {
  readonly projections: readonly Projection[]
  readonly unknown: boolean
  readonly fallbackFocus: (() => void) | undefined
}

function IndicatorBody({ projections, unknown, fallbackFocus }: IndicatorBodyProps): React.JSX.Element {
  const reveals = useReveals()
  const { setMode } = useAppNav()
  const inGm = useWorkbenchActive()
  const { doc } = useCanvasState()
  const [open, setOpen] = React.useState(false)
  const listId = React.useId()
  const rootRef = React.useRef<HTMLDivElement>(null)
  const toggleRef = React.useRef<HTMLButtonElement>(null)
  const fallbackRef = React.useRef(fallbackFocus)
  React.useLayoutEffect(() => {
    fallbackRef.current = fallbackFocus
  })

  // If the last Stop removes the indicator while it holds focus, focus must not fall to <body>.
  React.useLayoutEffect(() => {
    const node = rootRef.current
    return () => {
      if (node?.contains(document.activeElement) === true) fallbackRef.current?.()
    }
  }, [])

  // A press outside the list closes it, moving no focus.
  React.useEffect(() => {
    if (!open) return undefined
    const away = (event: PointerEvent): void => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target) !== true) setOpen(false)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])

  const canvasDocument = doc.kind === 'open' ? doc.document.document_id : null
  const stopping = (documentId: string): boolean => reveals.stopping.has(documentId) || reveals.stopping.has(STOP_ALL)
  const anyStopping = reveals.stopping.size > 0
  const stale = projections.find((entry) => entry.staleText)

  function openSheet(documentId: string, opener: Element): void {
    // The opener is the control pressed, so focus returns to it when the sheet closes.
    setOpen(false)
    reveals.openSheet(documentId, opener, canvasDocument)
  }

  function choose(entry: Projection, opener: Element): void {
    // X-9: outside the GM channel a projection can only take the GM back; nothing is opened from here.
    if (inGm) openSheet(entry.documentId, opener)
    else {
      setOpen(false)
      setMode('gm')
    }
  }

  function stopOne(entry: Projection): void {
    reveals.stop(entry.documentId, entry.title)
    // The row goes when the Stop lands: keep focus on something that stays.
    toggleRef.current?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== 'Escape' || !open) return
    // Handled here, so the shell's document-level drawer Escape never acts as well.
    event.preventDefault()
    setOpen(false)
    toggleRef.current?.focus()
  }

  return (
    <div
      ref={rootRef}
      className="reveal-indicator"
      role="group"
      aria-label={REVEAL_COPY.indicatorLabel}
      data-state={unknown ? 'unknown' : 'live'}
      data-mode={inGm ? undefined : 'stop-only'}
      onKeyDown={onKeyDown}
    >
      {unknown ? (
        <span className="reveal-indicator__text">{REVEAL_COPY.indicatorUnknown}</span>
      ) : (
        <button
          ref={toggleRef}
          type="button"
          className="reveal-indicator__summary"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="material-symbols-rounded reveal-indicator__icon" aria-hidden="true">
            visibility
          </span>
          <span className="reveal-indicator__text">{projectionSummary(projections)}</span>
        </button>
      )}

      {!unknown && stale !== undefined && (
        <span className="reveal-indicator__note">{REVEAL_COPY.earlierVersionShort}</span>
      )}
      {!unknown && stale !== undefined && inGm && (
        <button
          type="button"
          className="reveal-indicator__button"
          onClick={(event) => openSheet(stale.documentId, event.currentTarget)}
        >
          {REVEAL_COPY.updateEllipsis}
        </button>
      )}
      {anyStopping && reveals.stopFailed && <span className="reveal-indicator__note" data-tone="error">{REVEAL_COPY.stopFailedShort}</span>}

      <button
        type="button"
        className="reveal-indicator__button reveal-indicator__button--stop"
        onClick={() => reveals.stop(STOP_ALL, REVEAL_COPY.everything)}
      >
        {REVEAL_COPY.stopAll(unknown ? null : projections.length)}
      </button>

      {open && !unknown && (
        <ul id={listId} className="reveal-indicator__list" aria-label={REVEAL_COPY.indicatorListLabel}>
          {projections.map((entry) => (
            <li key={entry.documentId} className="reveal-indicator__row">
              <button
                type="button"
                className="reveal-indicator__button reveal-indicator__open"
                aria-label={inGm ? undefined : REVEAL_COPY.goToGm(`${entry.title} (${entry.who})`)}
                onClick={(event) => choose(entry, event.currentTarget)}
              >
                {`${entry.title} (${entry.who})`}
              </button>
              {entry.staleText && <span className="reveal-indicator__note">{REVEAL_COPY.earlierVersionShort}</span>}
              {entry.waiting && <span className="reveal-indicator__note">{REVEAL_COPY.waitingNote}</span>}
              {stopping(entry.documentId) && !reveals.stopFailed && <span className="reveal-indicator__note">{REVEAL_COPY.stopping}</span>}
              <button
                type="button"
                className="reveal-indicator__button reveal-indicator__button--stop"
                aria-label={REVEAL_COPY.stopShowingTitle(entry.title)}
                onClick={() => stopOne(entry)}
              >
                {REVEAL_COPY.stopShort}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
