/**
 * TavernCampaignCard -- one owned campaign on the tavern screen
 * (agent-forge-harness-30c, PR-1; spec section 3.3).
 *
 * A non-interactive container (ID-5): it holds buttons, so it is never one
 * itself, and nothing on its root is clickable. It names its group by the
 * campaign's name, and each repeated action carries that name in an sr-only
 * suffix so the visible label starts the accessible name (ID-20, WCAG 2.5.3).
 *
 * Three variants, all fed by the server's facts and none of them decided here:
 * - active: Prep, and Start Session shown locked (ID-6) -- `aria-disabled`,
 *   described by "Running a live table is part of Loremaster", and a press does
 *   nothing: no dialog, no request, no navigation.
 * - dormant (the server's `dormant`, over 30 days): an overline reading
 *   "Dormant · no activity since {date}" (ID-7) on a secondary-container
 *   surface with a decorative gold-leaf border (ID-18), Prep, and Mark
 *   concluded. There is no Start Session.
 * - concluded: Prep and Reopen (ID-9). There is no Start Session.
 */

import * as React from 'react'
import { Avatar } from '../ds/Avatar'
import { Badge } from '../ds/Badge'
import { Card } from '../ds/Card'
import { Chip } from '../ds/Chip'
import type { Campaign } from '../gm/contracts'
import { PendingButton } from './CampaignPicker'
import { TavernActionButton } from './TavernActionButton'
import { BADGE_COPY, SYSTEM_LABELS, seatChipLabel, tavernDate } from './tavernOrder'
import './TavernCampaignCard.css'

const LOCK_NOTE = 'Running a live table is part of Loremaster'

export type TavernCardVariant = 'active' | 'dormant' | 'concluded'

export interface TavernCampaignCardProps {
  campaign: Campaign
  variant: TavernCardVariant
  onPrep: () => void
  onConclude: () => void
  onReopen: () => void
  /** Its own conclude or reopen is in flight: `aria-disabled`, and a press does nothing. */
  busy: boolean
  /** Lets the screen focus this card's Prep. */
  prepRef?: React.Ref<HTMLButtonElement>
  /** Where focus goes if the pending button leaves the DOM while focused: the page heading. */
  landing: React.RefObject<HTMLElement | null>
  now: Date
}

export function TavernCampaignCard({
  campaign,
  variant,
  onPrep,
  onConclude,
  onReopen,
  busy,
  prepRef,
  landing,
  now,
}: TavernCampaignCardProps): React.JSX.Element {
  const ids = React.useId()
  const nameId = `${ids}-name`
  const lockId = `${ids}-lock`
  const seats = seatChipLabel(campaign.seat_count)
  const badge = campaign.badge === null ? null : BADGE_COPY[campaign.badge]
  // The separating space is a text node of its own, outside the hidden span, so
  // the name reads "Prep {name}" with or without the stylesheet applied.
  const suffix = <>{' '}<span className="tavern__sr-only">{campaign.name}</span></>

  return (
    <Card
      variant="elevated"
      role="group"
      aria-labelledby={nameId}
      className={`tavern-card tavern-card--${variant}`}
    >
      <div className="tavern-card__head">
        <Avatar icon={campaign.avatar_icon} tone={campaign.avatar_tone} size={46} aria-hidden="true" />
        <div className="tavern-card__title">
          {variant === 'dormant' && (
            <p className="tavern-card__overline">
              <span className="material-symbols-rounded" aria-hidden="true">history_toggle_off</span>
              {`Dormant · no activity since ${tavernDate(campaign.last_activity_at, now)}`}
            </p>
          )}
          <div className="tavern-card__name-row">
            <h2 id={nameId} className="tavern-card__name">{campaign.name}</h2>
            {badge !== null && <Badge tone={badge.tone}>{badge.label}</Badge>}
          </div>
          {campaign.tone !== null && <p className="tavern-card__tone">{campaign.tone}</p>}
        </div>
      </div>

      <div className="tavern-card__chips">
        <Chip type="assist" icon="menu_book" label={SYSTEM_LABELS[campaign.game_system]} />
        {seats !== null && <Chip type="assist" icon="groups" label={seats} />}
      </div>

      <div className="tavern-card__actions">
        <TavernActionButton variant="tonal" icon="edit_note" onPress={onPrep} buttonRef={prepRef}>
          Prep{suffix}
        </TavernActionButton>
        {variant === 'active' && (
          <>
            <TavernActionButton
              variant="outlined"
              icon="lock"
              trailingIcon="play_arrow"
              ariaDisabled
              ariaDescribedBy={lockId}
              onPress={() => {}}
            >
              Start Session{suffix}
            </TavernActionButton>
            <span id={lockId} className="tavern__sr-only">{LOCK_NOTE}</span>
          </>
        )}
        {variant === 'dormant' && (
          <PendingButton busy={busy} landing={landing} onPress={onConclude}>
            Mark concluded{suffix}
          </PendingButton>
        )}
        {variant === 'concluded' && (
          <PendingButton busy={busy} landing={landing} onPress={onReopen}>
            Reopen{suffix}
          </PendingButton>
        )}
      </div>
    </Card>
  )
}
