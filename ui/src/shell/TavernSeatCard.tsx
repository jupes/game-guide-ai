/**
 * TavernSeatCard -- one of the caller's own seats on the tavern screen
 * (agent-forge-harness-30c, PR-2; ID-17, ID-24).
 *
 * The card says exactly one thing about the seat's standing:
 * - concluded: "This table has concluded";
 * - not yet confirmed (D-12): "Waiting for your GM to confirm your seat", whatever
 *   the table is doing, because an unconfirmed seat cannot join;
 * - confirmed and live: the "Live now" badge and a link to the player table;
 * - otherwise "Last played {date}" when the table has met.
 *
 * Only what `PlayerSeat` carries is shown: the table's name, tone and system and
 * the player's own alias. No seat count and no other player (cfx, SEC-43).
 */

import * as React from 'react'
import { Avatar } from '../ds/Avatar'
import { Badge } from '../ds/Badge'
import { Card } from '../ds/Card'
import { Chip } from '../ds/Chip'
import type { PlayerSeat } from '../gm/contracts'
import { tableAddress } from '../table/address'
import { SYSTEM_LABELS, tavernDate } from './tavernOrder'
import './TavernCampaignCard.css'
import './TavernSeatCard.css'

export const LIVE_NOW = 'Live now'
export const WAITING_FOR_GM = 'Waiting for your GM to confirm your seat'
export const SEAT_CONCLUDED = 'This table has concluded'

export interface TavernSeatCardProps {
  seat: PlayerSeat
  now: Date
  /** Focus the first newly revealed seat without adding it to the Tab order. */
  headingRef?: React.RefObject<HTMLHeadingElement | null>
}

function Note({ icon, children }: { icon: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <p className="tavern-seat__note">
      <span className="material-symbols-rounded" aria-hidden="true">{icon}</span>
      {children}
    </p>
  )
}

export function TavernSeatCard({ seat, now, headingRef }: TavernSeatCardProps): React.JSX.Element {
  const nameId = React.useId()
  const live = seat.live && seat.confirmed && !seat.concluded
  const state = seat.concluded ? 'concluded' : !seat.confirmed ? 'waiting' : live ? 'live' : 'quiet'

  return (
    <Card variant="elevated" role="group" aria-labelledby={nameId} className={`tavern-card tavern-seat tavern-seat--${state}`}>
      <div className="tavern-card__head">
        <Avatar icon={seat.avatar_icon} tone={seat.avatar_tone} size={46} aria-hidden="true" />
        <div className="tavern-card__title">
          <div className="tavern-card__name-row">
            <h2 id={nameId} className="tavern-card__name" ref={headingRef} tabIndex={headingRef ? -1 : undefined}>
              {seat.campaign_name}
            </h2>
            {live && <Badge tone="nat20">{LIVE_NOW}</Badge>}
          </div>
          {seat.tone !== null && <p className="tavern-card__tone">{seat.tone}</p>}
        </div>
      </div>

      <p className="tavern-seat__alias">{`Playing as ${seat.alias}`}</p>

      <div className="tavern-card__chips">
        <Chip type="assist" icon="menu_book" label={SYSTEM_LABELS[seat.game_system]} />
      </div>

      {state === 'concluded' && <Note icon="inventory_2">{SEAT_CONCLUDED}</Note>}
      {state === 'waiting' && <Note icon="hourglass_empty">{WAITING_FOR_GM}</Note>}
      {state === 'quiet' && seat.last_played_at !== null && (
        <Note icon="history">{`Last played ${tavernDate(seat.last_played_at, now)}`}</Note>
      )}
      {live && (
        <a className="tavern-seat__join" href={tableAddress(seat.campaign_id)} aria-label={`Join table ${seat.campaign_name}`}>
          Join table
        </a>
      )}
    </Card>
  )
}
