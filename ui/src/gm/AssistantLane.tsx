/**
 * AssistantLane — the third lane of the GM thread (1kg.3.2).
 *
 * `ChatMessage` owns two lanes, narration and player turns; this is the third,
 * where an assistant answer or a tool result sits beneath the turn that asked
 * for it. Decisions RAIL-15 to RAIL-24 and RAIL-27 are its specification and
 * §3.4's table is, row for row, what it renders.
 *
 * Four differences from the handoff component, each a decision rather than a
 * preference:
 *
 *  1. **No `children` and no `status` prop.** The handoff renders `children`
 *     under whatever status it is given, so a `working` header can sit above the
 *     previous result. RAIL-15 and RAIL-18 forbid that, so this lane takes a
 *     `ToolInvocation` and derives a `LaneView` (`laneState.ts`) in which only
 *     the `done` member carries a result at all. Stale children are a type
 *     error, not a review note.
 *  2. **`cancelled` and `unknown`.** The handoff has three statuses; the record
 *     adds RAIL-22's `cancelled` and RAIL-21's client-only `unknown`, which is
 *     what a hydrated `working` entry and a lost response become. The lane never
 *     re-runs anything by itself (X-1).
 *  3. **Suggestions arm, they never run.** `onSuggestion` in the handoff is
 *     wired to `runTool`. RAIL-8's chip hands back a command, an optional
 *     prefilled brief and the entry it came from; the composer and the GM do
 *     the rest.
 *  4. **One polite live region, always mounted.** RAIL-15 wants the working
 *     label announced and RAIL-17 wants `<label> finished` announced once.
 *     A region that mounts with each state would announce a hydrated timeline
 *     all at once, so there is exactly one, its text is derived from the view,
 *     and an unchanged re-render mutates nothing (STATE-7).
 *
 * The lane is controlled and side-effect-free apart from its own 30 s timer: no
 * fetching, no polling, no store. Running a tool, polling a status (RAIL-21) and
 * cancelling on the server belong to 1kg.4.x, and hydrating a timeline to
 * 1kg.3.4.
 *
 * Privacy, X-7: prose, a brief and a document title are GM-private. They are
 * rendered and nothing else — never a key, a data attribute, a storage entry or
 * a log line.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { Chip } from '../ds/Chip'
import { StatBlockCard } from '../ds/StatBlockCard'
import { AssistantDocumentLink } from './AssistantDocumentLink'
import { AssistantText } from './AssistantText'
import { toStatBlockCardProps } from './adapters'
import type { DocumentLink, ToolId, ToolInvocation, ToolResult } from './contracts'
import {
  LANE_ACTION_LABEL,
  LANE_COPY,
  STILL_WORKING_MS,
  documentLinkMeta,
  normaliseLane,
  realTimer,
} from './laneState'
import type { ArmedSuggestion, LaneActionId, LaneTimer, LaneView } from './laneState'
import type { ToolAvailability } from './registry'
import './AssistantLane.css'

export interface AssistantLaneProps {
  /**
   * The status resource behind this lane. `null` stands for a stored entry this
   * client could not read — an `opaque` timeline entry, or a payload that failed
   * `parseToolInvocation` — and renders RAIL-24's neutral placeholder.
   */
  invocation: ToolInvocation | null
  /** RAIL-21: this entry came from history, so a `working` status is `unknown`. */
  hydrated?: boolean
  /** RAIL-21: the response was lost, or the client waited past 120 s. */
  lost?: boolean
  /**
   * RAIL-8: which tools a suggestion may target. Omitted means none, which is
   * the state before the capability lookup answers and after it fails (AE-58):
   * a chip that cannot run is not offered.
   */
  availability?: Readonly<Partial<Record<ToolId, ToolAvailability>>>
  /** RAIL-8: the timeline entry a suggestion cites as its source. */
  sourceEntryId?: string | null
  /** The lane's kicker. @default 'Aetheril · Assistant' */
  author?: string
  /** Material Symbols Rounded ligature, decorative. @default 'castle' */
  icon?: string
  /**
   * CANVAS-3. Required, because a document result always renders its link and a
   * link with no destination is a dead end (STATE-2). Whether the canvas also
   * opens by itself is CANVAS-1's, and the caller's.
   */
  onOpenDocument: (link: DocumentLink) => void
  /** RAIL-8. Arms the composer; it must not run anything. */
  onArmSuggestion?: (armed: ArmedSuggestion) => void
  /** RAIL-15, RAIL-21. */
  onCancel?: () => void
  /** RAIL-18: re-sends the same invocation id. */
  onRetry?: () => void
  /** RAIL-19: re-arms the composer with the original command and brief. */
  onEditBrief?: () => void
  /** RAIL-21: reads status by id. Never re-runs. */
  onCheckAgain?: () => void
  /** RAIL-22: arms the composer with the same command and brief. */
  onRunAgain?: () => void
  /**
   * RAIL-15's 30 s clock, injected so tests need not wait. Must be referentially
   * stable; the default is a module constant.
   */
  timer?: LaneTimer
  className?: string
}

/** True unless the browser asks for reduced motion (the DiceRoll guard, same reasoning). */
function motionAllowed(): boolean {
  if (typeof window === 'undefined') return false
  if (typeof window.matchMedia !== 'function') return true
  return !window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * RAIL-15's `Still working…`. It is this client's own stopwatch — the server
 * never says "still" — so it lives here and runs through the injected timer.
 *
 * The state remembers *which* run passed 30 s rather than a bare boolean, so
 * the reset is a derivation instead of a second render. That matters for
 * RAIL-18: Try again re-sends the same invocation id, so the attempt number is
 * part of the run's identity and a retry starts the half-minute over.
 */
function useStillWorking(waitingFor: string | null, timer: LaneTimer): boolean {
  const [elapsedFor, setElapsedFor] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (waitingFor === null) return
    return timer.schedule(STILL_WORKING_MS, () => setElapsedFor(waitingFor))
  }, [waitingFor, timer])
  return waitingFor !== null && elapsedFor === waitingFor
}

function ResultBody({
  result,
  onOpenDocument,
}: {
  result: ToolResult
  onOpenDocument: (link: DocumentLink) => void
}): React.JSX.Element {
  switch (result.result_kind) {
    case 'card':
      // The lane supplies the chrome, so the card comes in compact.
      return <StatBlockCard density="compact" {...toStatBlockCardProps(result.card.stat_block)} />
    case 'document':
      return documentLinkMeta(result.document) === null
        ? <p className="assistant-lane__placeholder">{LANE_COPY.newerVersion}</p>
        : <AssistantDocumentLink link={result.document} onOpen={onOpenDocument} />
    case 'media':
      // RAIL-24, X-8: a kind this bead does not own is neutral and never a
      // guess at another card. The media card and its audio cue are 1kg.8.4's.
      return <p className="assistant-lane__placeholder">{LANE_COPY.unsupportedResult}</p>
  }
}

export function AssistantLane({
  invocation,
  hydrated = false,
  lost = false,
  availability,
  sourceEntryId = null,
  author = LANE_COPY.author,
  icon = 'castle',
  onOpenDocument,
  onArmSuggestion,
  onCancel,
  onRetry,
  onEditBrief,
  onCheckAgain,
  onRunAgain,
  timer = realTimer,
  className,
}: AssistantLaneProps): React.JSX.Element {
  // Only a lane this client is actually watching runs the stopwatch: a hydrated
  // or lost run is RAIL-21's `unknown`, which is checked on, not timed.
  const waitingFor =
    invocation !== null && invocation.status === 'working' && !hydrated && !lost
      ? `${invocation.invocation_id}#${invocation.attempt}`
      : null
  const stillWorking = useStillWorking(waitingFor, timer)

  const view = normaliseLane(invocation, { hydrated, lost, stillWorking, availability, sourceEntryId })

  const rootRef = React.useRef<HTMLDivElement>(null)
  const headingRef = React.useRef<HTMLDivElement>(null)
  const heldFocus = React.useRef(false)
  const lastState = React.useRef<LaneView['state']>(view.state)

  // LAYOUT-6: focus moves only when its element ceases to exist, and then to
  // the view's heading. Pressing Cancel and watching the run finish must not
  // drop the GM on <body>; everything else — CANVAS-1's "focus stays in the
  // composer" above all — leaves focus exactly where it was.
  //
  // The flag is kept by focus events rather than read at commit time, because
  // focus moves between renders: a GM clicks Cancel, and only the *next* state
  // change asks whether they were standing on it. Moving focus away fires
  // `focusout` with the new element; removing a focused element fires nothing
  // and leaves `document.activeElement` on `<body>`, which is exactly the case
  // worth recovering from.
  React.useLayoutEffect(() => {
    const changed = lastState.current !== view.state
    lastState.current = view.state
    const active = document.activeElement
    if (changed && heldFocus.current && (active === null || active === document.body)) {
      headingRef.current?.focus()
    }
  })

  const handlers: Readonly<Record<LaneActionId, (() => void) | undefined>> = {
    cancel: onCancel,
    'try-again': onRetry,
    'edit-brief': onEditBrief,
    'check-again': onCheckAgain,
    'run-again': onRunAgain,
  }
  const actions =
    view.state === 'done' || view.state === 'unreadable'
      ? []
      : view.actions.filter((id) => handlers[id] !== undefined)

  const waiting = view.state === 'working' || view.state === 'checking'
  // `done` announces without showing; every other state shows what it announces.
  const visibleStatus = statusText(view)
  const announcement = view.state === 'done' ? view.announcement : visibleStatus

  return (
    <div
      ref={rootRef}
      className={['assistant-lane', className].filter(Boolean).join(' ')}
      data-state={view.state}
      onFocus={() => {
        heldFocus.current = true
      }}
      onBlur={(event) => {
        const next = event.relatedTarget
        if (next !== null && rootRef.current?.contains(next) !== true) heldFocus.current = false
      }}
    >
      {/* `tabIndex={-1}` only so the focus rule above has somewhere to land; the
          heading is never in the tab order. */}
      <div className="assistant-lane__header" ref={headingRef} tabIndex={-1}>
        <span className="material-symbols-rounded assistant-lane__icon" aria-hidden="true">
          {icon}
        </span>
        <span className="assistant-lane__author">{author}</span>
        {view.state !== 'unreadable' && <span className="assistant-lane__tool">{view.tool.label}</span>}
      </div>

      <div
        className={
          'assistant-lane__progress'
          + (visibleStatus === null ? ' assistant-lane__progress--announce-only' : '')
        }
      >
        {waiting && (
          <span
            className={'assistant-lane__dots' + (motionAllowed() ? ' assistant-lane__dots--animated' : '')}
            aria-hidden="true"
          >
            <span className="assistant-lane__dot" />
            <span className="assistant-lane__dot" />
            <span className="assistant-lane__dot" />
          </span>
        )}
        {view.state === 'error' && (
          <span className="material-symbols-rounded assistant-lane__error-icon" aria-hidden="true">
            error
          </span>
        )}
        {/* The one live region. Always mounted, so a transition mutates it and a
            re-render with the same view does not — and a hydrating timeline,
            whose regions arrive already populated, announces nothing at all.
            Polite rather than an alert: the error ends an operation the GM
            started and is one of the two announcements STATE-7 allows, so it
            should not cut across whatever they are typing. */}
        <span role="status" aria-live="polite" className="assistant-lane__status">
          {announcement !== null
            && (visibleStatus === null
              ? <span className="assistant-lane__sr-only">{announcement}</span>
              : <span className="assistant-lane__status-text">{announcement}</span>)}
        </span>
      </div>

      {view.state === 'done' && view.note !== null && (
        <p className="assistant-lane__note">{view.note}</p>
      )}

      {view.state === 'unreadable' && <p className="assistant-lane__placeholder">{view.placeholder}</p>}

      {view.state === 'done' && (
        <div className="assistant-lane__body">
          {view.result.prose.trim() !== '' && <AssistantText source={view.result.prose} />}
          <ResultBody result={view.result} onOpenDocument={onOpenDocument} />
        </div>
      )}

      {actions.length > 0 && (
        <div className="assistant-lane__actions">
          {actions.map((id) => (
            <Button key={id} variant="text" size="small" onClick={handlers[id]}>
              {LANE_ACTION_LABEL[id]}
            </Button>
          ))}
        </div>
      )}

      {/* No handler, no chips: a suggestion that cannot arm anything is a dead end (STATE-2). */}
      {view.state === 'done' && onArmSuggestion !== undefined && view.chips.length > 0 && (
        <div className="assistant-lane__suggestions">
          {view.chips.map((chip) => (
            <Chip
              key={chip.key}
              type="suggestion"
              label={chip.label}
              icon={chip.icon}
              onClick={() => onArmSuggestion(chip.armed)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** The visible status line, for the states that have one. */
function statusText(view: LaneView): string | null {
  switch (view.state) {
    case 'working':
    case 'checking':
    case 'cancelled':
      return view.status
    case 'error':
      return view.message
    default:
      return null
  }
}
