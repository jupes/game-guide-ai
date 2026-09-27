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
 *     `Checking on…` and never re-runs. A plain turn is answered here too
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
 */

import * as React from 'react'
import { ChatMessage } from '../ds/ChatMessage'
import { DiceRoll } from '../ds/DiceRoll'
import { SpellCard } from '../ds/SpellCard'
import { StatBlockCard } from '../ds/StatBlockCard'
import { SourceList } from '../components/SourceList'
import { parseDiceNotation } from '../shell/diceNotation'
import { AssistantLane } from './AssistantLane'
import { AssistantText } from './AssistantText'
import { toSpellCardProps, toStatBlockCardProps } from './adapters'
import type { DocumentLink } from './contracts'
import type { AnswerState, GmTurn, LaneAnswer } from './gmTimeline'
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

/** No canvas is mounted in the shell yet (1kg.6.3), and no tool can return a
 * document before the invocation API (1kg.4.1), so nothing reaches this. */
const noCanvas = (): void => {}

export interface GmThreadProps {
  turns: readonly GmTurn[]
  /** CANVAS-3: Open in canvas. */
  onOpenDocument?: (link: DocumentLink) => void
}

export function GmThread({ turns, onOpenDocument = noCanvas }: GmThreadProps): React.JSX.Element {
  return (
    <>
      {turns.map((turn) => (
        <div key={turn.key} className="gm-thread__exchange">
          <Narration turn={turn} />
          <Outcome turn={turn} onOpenDocument={onOpenDocument} />
        </div>
      ))}
    </>
  )
}

function Narration({ turn }: { turn: GmTurn }): React.JSX.Element | null {
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
  turn: GmTurn
  onOpenDocument: (link: DocumentLink) => void
}): React.JSX.Element | null {
  switch (turn.kind) {
    case 'chat':
      return <AnswerLane answer={turn.answer} />
    case 'tool':
      return (
        <AssistantLane
          invocation={turn.invocation}
          hydrated
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
function AnswerLane({ answer }: { answer: AnswerState }): React.JSX.Element | null {
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
          <AnswerBody answer={answer.answer} />
        </LaneFrame>
      )
  }
}

/**
 * Prose, then what was lifted out of it, then the notice, then the evidence —
 * the order a GM reads, and the order it is announced. `answerable` of `null`
 * (not recorded) earns neither the creative notice nor citations.
 */
function AnswerBody({ answer }: { answer: LaneAnswer }): React.JSX.Element {
  const grounded = answer.answerable === true
  const dice = grounded ? parseDiceNotation(answer.text) : null
  return (
    <div className="assistant-lane__body">
      {answer.text.trim() !== '' && <AssistantText source={answer.text} />}
      {answer.spell_content && <SpellCard {...toSpellCardProps(answer.spell_content)} density="compact" />}
      {answer.stat_block && <StatBlockCard {...toStatBlockCardProps(answer.stat_block)} density="compact" />}
      {answer.answerable === false && <p className="gm-thread__creative">{CREATIVE_NOTICE}</p>}
      {dice && (
        <div className="gm-thread__dice">
          <DiceRoll die={dice.die} value={dice.value} modifier={dice.modifier} />
        </div>
      )}
      {grounded && answer.sources !== null && answer.sources.length > 0 && (
        <div className="gm-thread__sources">
          <SourceList sources={answer.sources} />
        </div>
      )}
    </div>
  )
}
