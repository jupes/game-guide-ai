/**
 * TavernNoSeats -- the tavern for an account with nothing to show yet
 * (agent-forge-harness-30c, PR-2; the spec's E-1, ID-25).
 *
 * A player whose read of their seats came back empty has no campaign of their
 * own and no table: the screen says so truthfully and offers the one thing that
 * works today, "Ask the Sage". The copy is the spec's E-1 as drawn. Its
 * "Tell me when campaigns open" is left out: nothing records such a request, so
 * a button for it would promise what the product cannot do.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import './TavernNoSeats.css'

export const NO_SEATS_TITLE = 'Campaigns are being built'
export const NO_SEATS_BODY =
  'Prep, documents and the live table are coming. Until then, the Sage, the Spell Archivist and the Rules Arbiter answer from the books — that part works today.'

export interface TavernNoSeatsProps {
  onAskTheSage: () => void
  /** The id of the panel's title, which names the panel's region. */
  titleId: string
}

export function TavernNoSeats({ onAskTheSage, titleId }: TavernNoSeatsProps): React.JSX.Element {
  return (
    <section className="tavern-no-seats" aria-labelledby={titleId}>
      <span className="material-symbols-rounded tavern-no-seats__icon" aria-hidden="true">auto_stories</span>
      <h2 id={titleId} className="tavern-no-seats__title">{NO_SEATS_TITLE}</h2>
      <p className="tavern-no-seats__body">{NO_SEATS_BODY}</p>
      <div className="tavern-no-seats__actions">
        <Button variant="filled" icon="auto_stories" onClick={onAskTheSage}>Ask the Sage</Button>
      </div>
    </section>
  )
}
