/**
 * BeginAnewCard -- start a campaign from the tavern (agent-forge-harness-30c,
 * PR-1; ID-14).
 *
 * An outlined card, never `interactive`: it holds a form. It asks for a name
 * only (interactions ADR section 12.2 and A-31(d) supersede "a name and a
 * tone"); the create itself is `CreateCampaignForm`, extracted unchanged from
 * the picker.
 */

import * as React from 'react'
import { Card } from '../ds/Card'
import type { Campaign } from '../gm/contracts'
import { CreateCampaignForm } from './CreateCampaignForm'
import './BeginAnewCard.css'

const LINE = 'Only a name is required to start. Everything else can wait for the table.'

export interface BeginAnewCardProps {
  onCreated: (campaign: Campaign) => void
  onAnnounce: (text: string) => void
  nameFieldRef?: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>
}

export function BeginAnewCard({ onCreated, onAnnounce, nameFieldRef }: BeginAnewCardProps): React.JSX.Element {
  return (
    <Card variant="outlined" className="begin-anew">
      <div className="begin-anew__head">
        <span className="material-symbols-rounded begin-anew__icon" aria-hidden="true">add_circle</span>
        <h2 className="begin-anew__title">Begin anew</h2>
      </div>
      <p className="begin-anew__line">{LINE}</p>
      <CreateCampaignForm onCreated={onCreated} onAnnounce={onAnnounce} nameFieldRef={nameFieldRef} />
    </Card>
  )
}
