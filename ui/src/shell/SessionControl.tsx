/**
 * SessionControl -- Start and End for the selected campaign's live table
 * (agent-forge-harness-1kg.2.5.3; brief sections 7.9 and 11, the table-session
 * row of section 12.2). The workspace supplies the selected campaign's provider.
 *
 * End is never disabled (X-3): shown whenever a live session is known, whatever
 * else is pending, and a press while an End waits to retry sends it at once.
 * Start is one press; the provider sends one request per intent, so the button
 * stays mounted and focusable while it is in flight. A 403 offers no Retry.
 *
 * One live region (A-29, STATE-7): a status node mounted empty, one message at
 * a Start's press and one at its end, one when an End is acknowledged. Given the
 * host pane's `announce`, it mounts none and uses the pane's, so a pane never
 * holds two. Every state is also visible text; nothing is colour alone.
 */

import { useState } from 'react'
import { Badge } from '../ds/Badge'
import { Button } from '../ds/Button'
import { useTableSession, type SessionProblem, type StartOutcome, type TableSessionValue } from './tableSession'
import './SessionControl.css'

// Section 12.2's strings verbatim; the others inferred, one constant each, for `cub`.
const NONE = 'No session running'
const START = 'Start session'
const STARTING = 'Starting…'
const START_FAILED = "Couldn't start the session"
const END = 'End session'
const STARTED = 'Session started'
const ENDED = 'Session ended'
const LOADING = 'Loading session…'
const LOAD_FAILED = "Couldn't load the session"
const UNAVAILABLE = "That campaign isn't available."
const LIVE_ELSEWHERE = 'Your table is live in another campaign. End it there, then start this one.'
const ENDING = 'Ending…'
const END_RETRYING = "Couldn't end the session yet. Still trying — press End session to try now."
const RETRY = 'Retry'
const LIVE = 'Live'
const PAST_DUE = 'Session end time has passed'
const liveUntil = (time: string): string => `Live until ${time}`
const lastEnded = (time: string): string => `The last session ended at ${time}.`
const throttled = (seconds: number | null): string =>
  `${START_FAILED}: too many starts just now. ${seconds === null ? 'Try again in a moment.' : `Try again in ${seconds} seconds.`}`

const RETRYABLE: ReadonlySet<SessionProblem['kind']> = new Set(['load_failed', 'start_failed', 'throttled'])
const START_OFFERED: ReadonlySet<SessionProblem['kind']> = new Set(['start_refused', 'live_elsewhere'])

function clock(at: string): string {
  return new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date(at))
}

function problemText(problem: SessionProblem): string {
  switch (problem.kind) {
    case 'load_failed':
      return LOAD_FAILED
    case 'unavailable':
      return UNAVAILABLE
    case 'live_elsewhere':
      return LIVE_ELSEWHERE
    case 'throttled':
      return throttled(problem.retryAfterS)
    default:
      return START_FAILED
  }
}

export interface SessionControlProps {
  /** The session to show; the provider's when omitted (stories pass one). */
  readonly value?: TableSessionValue
  /** The host pane's announcer. Given one, this control mounts no live region. */
  readonly announce?: (message: string) => void
}

export function SessionControl({ value, announce }: SessionControlProps): React.JSX.Element {
  const fromProvider = useTableSession()
  const session = value ?? fromProvider
  const [message, setMessage] = useState('')
  const say = announce ?? setMessage
  const { state, problem, pending } = session
  const table = session.session
  const showEnd = table?.state === 'live'
  const live = session.liveSession !== null

  async function run(send: () => Promise<StartOutcome>): Promise<void> {
    if (pending !== 'start') say(STARTING)
    const outcome = await send()
    if (outcome === 'started') say(STARTED)
    else if (outcome !== 'skipped' && outcome !== 'stale' && outcome !== 'signed_out') say(START_FAILED)
  }

  async function end(): Promise<void> {
    if ((await session.end()) === 'ended') say(ENDED)
  }

  function retry(): void {
    if (problem?.kind === 'load_failed') void session.retry()
    else void run(session.retry)
  }

  let text: string
  if (pending === 'start') text = STARTING
  else if (pending === 'end') text = session.endRetrying ? END_RETRYING : ENDING
  else if (problem !== null) text = problemText(problem)
  else if (state === 'loading') text = LOADING
  else if (showEnd) text = live ? liveUntil(clock(table.endsAt)) : PAST_DUE
  else text = NONE
  const endedAt = state === 'ended' && problem === null ? (session.session?.endedAt ?? null) : null
  const offerStart =
    !showEnd && (state === 'none' || state === 'ended') && (problem === null || START_OFFERED.has(problem.kind))

  return (
    <div className="session-control">
      {state !== 'idle' && (
        <div className="session-control__group" role="group" aria-label="Table session">
          <div className="session-control__summary">
            {live && <Badge tone="verdigris">{LIVE}</Badge>}
            <p className="session-control__text">{text}</p>
            {endedAt !== null && <p className="session-control__detail">{lastEnded(clock(endedAt))}</p>}
          </div>
          <div className="session-control__actions">
            {offerStart && <Button onClick={() => void run(session.start)}>{START}</Button>}
            {problem !== null && RETRYABLE.has(problem.kind) && (
              <Button variant="outlined" onClick={retry}>
                {RETRY}
              </Button>
            )}
            {showEnd && (
              <Button variant="outlined" onClick={() => void end()}>
                {END}
              </Button>
            )}
          </div>
        </div>
      )}
      {announce === undefined && (
        <p className="session-control__sr-only" role="status">
          {message}
        </p>
      )}
    </div>
  )
}
