/**
 * CreateCampaignForm -- the "Campaign name" field and "Create campaign"
 * (agent-forge-harness-30c, PR-1; extracted unchanged from `CampaignPicker`).
 *
 * The behaviour is the picker's, moved so two screens can host it:
 * - Create is sent only by a press, never retried by the client (a create has
 *   no idempotency key), and `creating` makes a double submit one call; the
 *   provider is single-flight besides.
 * - A name the contract refuses reads `NAME_INVALID` on the field
 *   (`aria-invalid`, `aria-describedby`), focuses it, and is announced as well
 *   as described, because focus already on the field moves nowhere.
 * - The host owns the status node: this form only hands it text through
 *   `onAnnounce` -- an empty string when a create starts, then the outcome.
 *   A veto (a guard said no) says nothing more.
 * - On success the host decides what next (`onCreated`); the typed name is
 *   cleared first.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { TextField } from '../ds/TextField'
import type { Campaign } from '../gm/contracts'
import { useCampaign } from './campaignContext'
import './CampaignPicker.css'

export const CREATED = 'Campaign created'
const CREATE_FAILED = "Couldn't create the campaign"
const NAME_INVALID = 'Give the campaign a name of 1 to 120 characters on one line.'

export interface CreateCampaignFormProps {
  /** A campaign was made and selected. Focus and navigation are then the host's. */
  onCreated: (campaign: Campaign) => void
  /** The text for the host's one status node. */
  onAnnounce: (text: string) => void
  /** The name field, so a host control (the tavern's New Campaign) can focus it. */
  nameFieldRef?: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>
}

export function CreateCampaignForm({ onCreated, onAnnounce, nameFieldRef }: CreateCampaignFormProps): React.JSX.Element {
  const { createCampaign } = useCampaign()
  const ownRef = React.useRef<HTMLInputElement | HTMLTextAreaElement>(null)
  const nameField = nameFieldRef ?? ownRef
  const errorId = `${React.useId()}-error`
  const [name, setName] = React.useState('')
  const [creating, setCreating] = React.useState(false)
  const [nameError, setNameError] = React.useState<string | null>(null)

  const create = (event: React.FormEvent): void => {
    event.preventDefault()
    if (creating) return
    setCreating(true)
    // From the press on, the announcer is the create's: the list re-read a
    // failed create starts in the background is not announced.
    onAnnounce('')
    void createCampaign(name).then((outcome) => {
      setCreating(false)
      if (outcome.kind === 'vetoed') return
      if (outcome.kind === 'created') {
        setName('')
        setNameError(null)
        onAnnounce(CREATED)
        onCreated(outcome.campaign)
        return
      }
      // Announced as well as described: focus already on the field (an Enter)
      // moves nowhere, so nothing would re-read the new description.
      const problem = outcome.kind === 'invalid' ? NAME_INVALID : CREATE_FAILED
      setNameError(problem)
      onAnnounce(problem)
      nameField.current?.focus()
    })
  }

  return (
    <form className="campaign-picker__form" onSubmit={create} noValidate>
      <TextField
        ref={nameField}
        label="Campaign name"
        value={name}
        onChange={(event) => setName(event.target.value)}
        error={nameError !== null}
        aria-invalid={nameError !== null || undefined}
        aria-describedby={nameError === null ? undefined : errorId}
        fullWidth
      />
      <Button type="submit" disabled={creating}>Create campaign</Button>
      {nameError !== null && <p id={errorId} className="campaign-picker__message campaign-picker__error">{nameError}</p>}
    </form>
  )
}
