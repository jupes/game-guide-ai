/**
 * GmThread — the GM channel's transcript in three lanes (1kg.3.4).
 *
 *  1. **Narration.** The signed-in GM's turn is a `ChatMessage` in the narration
 *     role. A tool turn is its brief with the tool's label as a badge, and an
 *     empty optional brief is the tool's blurb in italics (RAIL-9).
 *  2. **Players.** v1 has no player or table turns (E-5, NG-12), so nothing
 *     renders on the player lane in this channel.
 *  3. **The assistant**, directly beneath the turn that asked. A tool entry is
 *     an `AssistantLane`, hydrated, so a stored `working` run is RAIL-21's
 *     `Checking on…` and never re-runs — unless it is a run this client is
 *     watching (a turn with `live`, from `pendingWork.ts`, 1kg.3.5): then it
 *     is RAIL-15's working lane, or RAIL-21's checking once `lost`. The thread
 *     still passes no lane actions (1kg.4.5). A plain turn is answered here too
 *     (RAIL-14): prose in the lane's sans, cards compact, citations compact, and
 *     the creative disclaimer kept.
 *
 * Each exchange is one element, turn first and outcome second, and nothing is
 * reordered by CSS — so the reading order a screen reader follows is the order
 * on screen. The lanes carry no live region of their own for a plain turn: the
 * pane announces a turn's start and its arrival on its one live region
 * (agent-forge-harness-ekf).
 *
 * It renders a `GmTurn[]` and nothing else, so a live turn and its reload are
 * drawn by the same code (`gmTimeline.ts`).
 *
 * **Load earlier** (1kg.3.6) is the one control this component owns outright:
 * a button at the top of the thread, present only while `hasEarlier` is true.
 * It fetches nothing itself — `ChatPane` owns `useGmTimeline` and wires its
 * `loadEarlier`/`loadingEarlier`/`earlierError` straight through as props.
 * When the last page lands and the control goes, keyboard focus moves to the
 * first exchange that arrived (tabIndex -1), as VersionList's Load more does
 * (1kg.3.7). Only a keyboard press scrolls to it; otherwise the reader's place,
 * which ChatPane holds still as the older turns arrive, stays where it is.
 *
 * **Session dividers** (1kg.3.5) are drawn between exchanges, never inside
 * one: a line of visible text with the boundary's `<time>`, between two
 * decorative rules. A divider is text, not a widget (I-11): no
 * `role="separator"` (its children would become presentational and hide the
 * label from assistive technology), no live region (a hydrated divider is not
 * news, A-29), and not in the Tab order. It takes `tabIndex={-1}` only so
 * Load earlier's hand-off above can land on it when it is the first item.
 */

import * as React from 'react'
import { ChatMessage } from '../ds/ChatMessage'
import { DiceRoll } from '../ds/DiceRoll'
import { SpellCard } from '../ds/SpellCard'
import { StatBlockCard } from '../ds/StatBlockCard'
import { SourceList } from '../components/SourceList'
import { SuggestionCards } from '../components/SuggestionCards'
import { parseDiceNotation } from '../shell/diceNotation'
import { AssistantLane } from './AssistantLane'
import { AssistantText } from './AssistantText'
import { toSpellCardProps, toStatBlockCardProps } from './adapters'
import type { ChatMode } from '../api'
import type { DocumentLink } from './contracts'
import { DIVIDER_COPY, formatDividerTime } from './gmTimeline'
import type { AnswerState, DividerTurn, ExchangeTurn, GmTurn, LaneAnswer } from './gmTimeline'
import { LANE_COPY } from './laneState'
import { toolById } from './registry'
import './AssistantLane.css'
import './GmThread.css'

/** The narration lane speaks for whoever is signed in. */
const GM_AUTHOR = 'You'
/** Today's wording, unchanged: a creative answer is labelled, never passed off as grounded. */
const CREATIVE_NOTICE = '✦ Creative — may include invented content not drawn from the sources.'
/** A plain turn's working line, in the pane's words — and what the pane
 * announces when a GM turn starts, since this lane carries no live region. */
export const PENDING_LABEL = 'Consulting the tomes…'

/** The default for a standalone mount with no canvas to open into. The shell's ChatPane
 * passes `onOpenDocument` (1kg.6.3), which opens the document in the Workbench canvas. */
const noCanvas = (): void => {}

export interface GmThreadProps {
  turns: readonly GmTurn[]
  /** CANVAS-3: Open in canvas. */
  onOpenDocument?: (link: DocumentLink) => void
  /** 1kg.3.6: an older page exists to walk to. Omitted (or false) draws no
   * control — a thread that fits in one page has nothing to load. */
  hasEarlier?: boolean
  /** A Load earlier walk is in flight. */
  loadingEarlier?: boolean
  /** A failed walk (§12.2). STATE-1: the thread above stays exactly as it was. */
  earlierError?: string | null
  onLoadEarlier?: () => void
  /** 1kg.3.5: how a divider writes its time. Defaults to the reader's locale
   * (`formatDividerTime`); tests pin a zone and a locale. */
  formatTime?: (iso: string) => string
}

export function GmThread({
  turns,
  onOpenDocument = noCanvas,
  hasEarlier = false,
  loadingEarlier = false,
  earlierError = null,
  onLoadEarlier,
  formatTime = formatDividerTime,
}: GmThreadProps): React.JSX.Element {
  const buttonRef = React.useRef<HTMLButtonElement>(null)
  const firstExchangeRef = React.useRef<HTMLDivElement>(null)
  // 1kg.3.7, VersionList's hand-off: a press made here, whether its walk has
  // been seen in flight (so focus moves only once that walk settles), and
  // whether the keyboard made it.
  const press = React.useRef<{ stage: 'pressed' | 'loading'; byKeyboard: boolean } | null>(null)

  React.useEffect(() => {
    const pressed = press.current
    if (pressed === null) return
    if (loadingEarlier) {
      press.current = { ...pressed, stage: 'loading' }
      return
    }
    if (pressed.stage !== 'loading') return
    press.current = null
    // Only focus the walk left nowhere — the control unmounted with the last
    // page, or a browser dropped it off the disabled button — never focus the
    // reader has since put somewhere else.
    const active = document.activeElement
    if (active !== null && active !== document.body) return
    // focus() scrolls its target into view, and ChatPane has just held still
    // the content the reader was looking at (1kg.3.6), so the hand-off must
    // not scroll it away (PR #136 review H1). Back on the control, focus only
    // repairs a browser dropping it off the disabled button: it never scrolls.
    // On the first turn that arrived, it scrolls there for a keyboard press,
    // whose reader follows the focus ring, and not for a pointer press.
    if (hasEarlier) buttonRef.current?.focus({ preventScroll: true })
    else firstExchangeRef.current?.focus({ preventScroll: !pressed.byKeyboard })
  }, [loadingEarlier, hasEarlier, turns])

  const handleLoadEarlier = React.useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      // A click made by Enter or Space counts no pointer clicks: detail is 0.
      press.current = { stage: 'pressed', byKeyboard: event.detail === 0 }
      onLoadEarlier?.()
    },
    [onLoadEarlier],
  )

  return (
    <>
      {hasEarlier && onLoadEarlier && (
        <LoadEarlier loading={loadingEarlier} error={earlierError} onLoadEarlier={handleLoadEarlier} buttonRef={buttonRef} />
      )}
      {turns.map((turn, index) =>
        turn.kind === 'divider' ? (
          <Divider
            key={turn.key}
            turn={turn}
            formatTime={formatTime}
            ref={index === 0 ? firstExchangeRef : undefined}
          />
        ) : (
          <div
            key={turn.key}
            className="gm-thread__exchange"
            tabIndex={-1}
            ref={index === 0 ? firstExchangeRef : undefined}
          >
            <Narration turn={turn} />
            <Outcome turn={turn} onOpenDocument={onOpenDocument} />
          </div>
        ),
      )}
    </>
  )
}

/**
 * The control at the top of the thread (1kg.3.6, ADR 12.2's GM-thread row):
 * continues the walk from the cursor `useGmTimeline` kept, prepending what it
 * finds above this control. It carries no live region of its own — starting
 * and finishing are announced once each on the pane's single announcer
 * (agent-forge-harness-ekf / agent-forge-harness-4oz), never by a second
 * `role="status"` here — and a failed walk leaves it in place as its own
 * retry (STATE-1, STATE-2): the same press resumes from the same cursor.
 */
function LoadEarlier({
  loading,
  error,
  onLoadEarlier,
  buttonRef,
}: {
  loading: boolean
  error: string | null
  onLoadEarlier: (event: React.MouseEvent<HTMLButtonElement>) => void
  buttonRef: React.Ref<HTMLButtonElement>
}): React.JSX.Element {
  return (
    <div className="gm-thread__load-earlier">
      {error !== null && <p className="gm-thread__load-earlier-error">{error}</p>}
      <button
        type="button"
        ref={buttonRef}
        className="gm-thread__load-earlier-button"
        onClick={onLoadEarlier}
        disabled={loading}
      >
        <span className="material-symbols-rounded" aria-hidden="true">
          {loading ? 'progress_activity' : 'expand_less'}
        </span>
        {loading ? 'Loading…' : 'Load earlier'}
      </button>
    </div>
  )
}

/**
 * A session boundary (1kg.3.5, I-10, I-11): the copy, then its `<time>`; a
 * quiet session's span reads "… to …" through a visually hidden word, never a
 * spoken dash. The rules are drawn with borders (they survive forced colours)
 * and are `aria-hidden`. `tabIndex={-1}`: reachable by Load earlier's hand-off
 * alone, never by Tab.
 */
function Divider({
  turn,
  formatTime,
  ref,
}: {
  turn: DividerTurn
  formatTime: (iso: string) => string
  ref?: React.Ref<HTMLDivElement>
}): React.JSX.Element {
  return (
    <div ref={ref} className="gm-thread__divider" data-boundary={turn.boundary} tabIndex={-1}>
      <span className="gm-thread__divider-rule" aria-hidden="true" />
      <p className="gm-thread__divider-label">
        {DIVIDER_COPY[turn.boundary]} <time dateTime={turn.at}>{formatTime(turn.at)}</time>
        {turn.boundary === 'span' && (
          <>
            {' '}
            <span aria-hidden="true">–</span>
            <span className="gm-thread__sr-only">to</span>{' '}
            <time dateTime={turn.endedAt}>{formatTime(turn.endedAt)}</time>
          </>
        )}
      </p>
      <span className="gm-thread__divider-rule" aria-hidden="true" />
    </div>
  )
}

function Narration({ turn }: { turn: ExchangeTurn }): React.JSX.Element | null {
  if (turn.kind === 'chat') {
    return turn.prompt === null ? null : <ChatMessage role="dm" author={GM_AUTHOR}>{turn.prompt}</ChatMessage>
  }
  if (turn.kind !== 'tool') return null
  // X-8: an id this bundle does not know gets no badge rather than a borrowed one.
  const tool = toolById(turn.invocation.tool_id)
  return (
    <ChatMessage role="dm" author={GM_AUTHOR}>
      {tool && <span className="gm-thread__tool">{tool.label}</span>}{' '}
      {turn.brief.trim() !== '' ? turn.brief : tool && <em>{tool.blurb}</em>}
    </ChatMessage>
  )
}

function Outcome({
  turn,
  onOpenDocument,
}: {
  turn: ExchangeTurn
  onOpenDocument: (link: DocumentLink) => void
}): React.JSX.Element | null {
  switch (turn.kind) {
    case 'chat':
      return <AnswerLane answer={turn.answer} mode={turn.mode} />
    case 'tool':
      // 1kg.3.5: a run the pending-work model holds is watched (RAIL-15's
      // working lane, or RAIL-21's checking once lost); any other is hydrated.
      return (
        <AssistantLane
          invocation={turn.invocation}
          hydrated={turn.live === undefined}
          lost={turn.live?.lost === true}
          sourceEntryId={turn.entryId}
          onOpenDocument={onOpenDocument}
        />
      )
    case 'unreadable':
      return <AssistantLane invocation={null} onOpenDocument={onOpenDocument} />
    case 'unsupported':
      return (
        <LaneFrame state="done">
          <p className="assistant-lane__placeholder">{LANE_COPY.unsupportedResult}</p>
        </LaneFrame>
      )
  }
}

/** The lane's chrome, shared with `AssistantLane` through its stylesheet. */
function LaneFrame({ state, children }: { state: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="assistant-lane" data-state={state}>
      <div className="assistant-lane__header">
        <span className="material-symbols-rounded assistant-lane__icon" aria-hidden="true">
          castle
        </span>
        <span className="assistant-lane__author">{LANE_COPY.author}</span>
      </div>
      {children}
    </div>
  )
}

/** RAIL-14: a plain GM turn is answered, and the answer sits in the lane. */
function AnswerLane({ answer, mode }: { answer: AnswerState; mode: ChatMode }): React.JSX.Element | null {
  switch (answer.state) {
    case 'none':
      return null
    case 'pending':
      return (
        <LaneFrame state="working">
          <div className="assistant-lane__progress">
            <span className="assistant-lane__dots" aria-hidden="true">
              <span className="assistant-lane__dot" />
              <span className="assistant-lane__dot" />
              <span className="assistant-lane__dot" />
            </span>
            <span className="assistant-lane__status-text">{PENDING_LABEL}</span>
          </div>
        </LaneFrame>
      )
    case 'failed':
      return (
        <LaneFrame state="error">
          <div className="assistant-lane__progress">
            <span className="material-symbols-rounded assistant-lane__error-icon" aria-hidden="true">
              error
            </span>
            <span className="assistant-lane__status-text">{answer.message}</span>
          </div>
        </LaneFrame>
      )
    case 'answered':
      return (
        <LaneFrame state="done">
          <AnswerBody answer={answer.answer} mode={mode} />
        </LaneFrame>
      )
  }
}

/**
 * Prose, then what was lifted out of it, then the notice, then a spell's usage
 * suggestions (in `ChatPane`'s order, apart from the card so quoted rules stay
 * visibly verbatim), then the evidence — the order a GM reads, and the order
 * it is announced. `answerable` of `null` (not recorded) earns neither the
 * creative notice nor citations.
 *
 * agent-forge-harness-ffz (pr120 review L-3): the creative notice is the GM
 * channel's own wording for the GM's own improvisation — it does not fit a
 * Sage or Rules entry hydrated into this thread (a mode chip keeps the same
 * conversation, so those entries can land here too). Neither Sage nor Rules
 * shows any such notice for an unanswerable reply (ChatPane.tsx), so a
 * non-`gm` entry gets the same silent treatment here, matching `answerable
 * === null`'s "no claim either way" rather than mislabeling it as invented.
 */
function AnswerBody({ answer, mode }: { answer: LaneAnswer; mode: ChatMode }): React.JSX.Element {
  const grounded = answer.answerable === true
  const dice = grounded ? parseDiceNotation(answer.text) : null
  return (
    <div className="assistant-lane__body">
      {answer.text.trim() !== '' && <AssistantText source={answer.text} />}
      {answer.spell_content && <SpellCard {...toSpellCardProps(answer.spell_content)} density="compact" />}
      {answer.stat_block && <StatBlockCard {...toStatBlockCardProps(answer.stat_block)} density="compact" />}
      {mode === 'gm' && answer.answerable === false && <p className="gm-thread__creative">{CREATIVE_NOTICE}</p>}
      {dice && (
        <div className="gm-thread__dice">
          <DiceRoll die={dice.die} value={dice.value} modifier={dice.modifier} />
        </div>
      )}
      <SuggestionCards suggestions={answer.suggestions} />
      {grounded && answer.sources !== null && answer.sources.length > 0 && (
        <div className="gm-thread__sources">
          <SourceList sources={answer.sources} />
        </div>
      )}
    </div>
  )
}
