import * as React from 'react'
import { TextField } from '../ds/TextField'
import {
  CONTRACT_VERSION,
  EMAIL_MAX_CHARS,
  PASSWORD_MAX_CHARS,
  SeatCreateRequestSchema,
  SeatOfferRequestSchema,
  type Seat,
} from '../gm/contracts'
import {
  confirmCampaignSeat,
  createCampaignSeat,
  offerCampaignSeat,
  removeCampaignSeat,
  renameCampaignSeat,
  type SeatWriteFailure,
} from './campaignApi'
import { LibraryDialog } from './LibraryDialog'

export type SeatAction =
  | { kind: 'add' }
  | { kind: 'offer' | 'confirm' | 'remove' | 'rename'; seat: Seat }

export interface SeatChange {
  message: string
  updated?: Seat
  removedId?: string
}

const WRITE_ERRORS: Record<SeatWriteFailure['kind'], string> = {
  invalid: 'Check the details and try again.',
  alias_taken: 'A participant already uses that display name.',
  reauth_failed: 'That password did not match. Try again.',
  throttled: 'Please wait a moment before trying again.',
  unauthorized: 'Your session expired. Sign in again to continue.',
  unavailable: 'This seat is unavailable.',
  failed: 'Couldn’t save the seat. Try again.',
}

export function ParticipantSeatDialog({
  campaignId, action, fetchImpl, returnFocus, onClose,
}: {
  campaignId: string
  action: SeatAction
  fetchImpl: typeof fetch
  returnFocus: () => void
  onClose: (change?: SeatChange) => void
}): React.JSX.Element {
  const [alias, setAlias] = React.useState(() => action.kind === 'rename' ? action.seat.alias : '')
  const [address, setAddress] = React.useState('')
  const [password, setPassword] = React.useState('')
  const [made, setMade] = React.useState<Seat | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const field = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const busyRef = React.useRef(false)
  const isOffer = action.kind === 'add' || action.kind === 'offer'
  const heading = action.kind === 'add' ? 'Offer a seat'
    : action.kind === 'offer' ? `Offer a seat for ${action.seat.alias}`
      : action.kind === 'confirm' ? `Confirm ${action.seat.alias}`
        : action.kind === 'rename' ? `Rename ${action.seat.alias}` : `Remove ${action.seat.alias}`
  const description = action.kind === 'confirm'
    ? 'Confirm that you know who accepted this seat. They can then receive private views you share with them.'
    : action.kind === 'remove'
      ? 'Removing this seat withdraws the offer or removes table access and stops any private view for this player. Their character sheet stays in the campaign.'
      : isOffer ? 'The player can see this offer when they sign in with this email address.' : undefined

  function showFailure(result: SeatWriteFailure): void {
    setError(WRITE_ERRORS[result.kind])
    if (result.kind === 'reauth_failed') {
      setPassword('')
      field.current?.focus()
    }
  }

  async function saveOffer(): Promise<void> {
    const email = SeatOfferRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, email: address.trim() })
    if (!email.success) {
      setError('Enter the player’s email address.')
      return
    }
    let seat = action.kind === 'offer' ? action.seat : made
    if (seat === null) {
      const name = SeatCreateRequestSchema.safeParse({ schema_version: CONTRACT_VERSION, alias: alias.trim() })
      if (!name.success) {
        setError('Enter a display name of 1 to 40 characters.')
        return
      }
      const created = await createCampaignSeat(campaignId, name.data.alias, fetchImpl)
      if (created.kind !== 'ok') {
        showFailure(created)
        return
      }
      seat = created.seat
      // If sending the offer fails, keep the created seat for an explicit retry.
      setMade(seat)
    }
    const result = await offerCampaignSeat(campaignId, seat.participant_id, email.data.email, fetchImpl)
    if (result.kind === 'ok') onClose({ message: 'Seat offer submitted.' })
    else if (result.kind === 'failed' && action.kind === 'add') {
      setError('The seat is saved. We couldn’t confirm the offer. Try sending it again.')
    } else showFailure(result)
  }

  async function submit(): Promise<void> {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      if (action.kind === 'confirm') {
        const result = await confirmCampaignSeat(campaignId, action.seat.participant_id, fetchImpl)
        if (result.kind === 'ok') onClose({ message: 'Participant confirmed.', updated: result.seat })
        else showFailure(result)
      } else if (action.kind === 'rename') {
        const result = await renameCampaignSeat(campaignId, action.seat.participant_id, alias.trim(), fetchImpl)
        if (result.kind === 'ok') onClose({ message: 'Display name updated.', updated: result.seat })
        else if (result.kind === 'invalid') setError('Enter a display name of 1 to 40 characters.')
        else showFailure(result)
      } else if (action.kind === 'remove') {
        if (password.length === 0) {
          setError('Enter your password to remove this seat.')
          field.current?.focus()
          return
        }
        const result = await removeCampaignSeat(campaignId, action.seat.participant_id, password, fetchImpl)
        if (result.kind === 'ok') onClose({ message: 'Seat removed.', removedId: action.seat.participant_id })
        else showFailure(result)
      } else {
        await saveOffer()
      }
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  return (
    <LibraryDialog
      heading={heading}
      body={description}
      busy={busy}
      destructive={action.kind === 'remove'}
      initialFocus={action.kind === 'confirm' ? 'cancel' : 'field'}
      fieldRef={field}
      cancelLabel="Cancel"
      confirmLabel={busy ? 'Saving…' : action.kind === 'remove' ? 'Remove seat'
        : action.kind === 'confirm' ? 'Confirm seat' : action.kind === 'rename' ? 'Save name' : 'Send offer'}
      error={error}
      returnFocus={returnFocus}
      onConfirm={() => void submit()}
      onCancel={() => onClose()}
    >
      {(action.kind === 'add' || action.kind === 'rename') && (
        <TextField
          label="Display name"
          ref={field}
          value={alias}
          disabled={busy || made !== null}
          onChange={(event) => setAlias(event.target.value)}
          fullWidth
        />
      )}
      {isOffer && (
        <TextField
          label="Player email"
          type="email"
          ref={action.kind === 'offer' ? field : undefined}
          value={address}
          maxLength={EMAIL_MAX_CHARS}
          autoComplete="off"
          disabled={busy}
          onChange={(event) => setAddress(event.target.value)}
          fullWidth
        />
      )}
      {action.kind === 'remove' && (
        <TextField
          label="Your password"
          type="password"
          ref={field}
          value={password}
          maxLength={PASSWORD_MAX_CHARS}
          autoComplete="current-password"
          disabled={busy}
          onChange={(event) => setPassword(event.target.value)}
          fullWidth
        />
      )}
    </LibraryDialog>
  )
}
