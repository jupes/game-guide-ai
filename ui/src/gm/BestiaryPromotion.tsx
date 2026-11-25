import * as React from 'react'
import { Button } from '../ds/Button'
import type { DocumentLink } from './contracts'
import { promoteMonsterCard, type CreateResult } from './documentApi'

const FAILURE_COPY: Record<Exclude<CreateResult['kind'], 'ok'>, string> = {
  failed: 'Couldn’t save to the Bestiary. Try again.',
  invalid: 'This card could not be saved.',
  limit: 'The account’s document storage is full.',
  throttled: 'Please wait a moment before trying again.',
  unauthorized: 'Sign in again to save this card.',
  unavailable: 'This card is unavailable.',
  unsupported: 'This card can’t be saved as an editable stat block.',
}

export function BestiaryPromotion({
  campaignId, conversationId, entryId, onSaved, onOpen,
}: {
  campaignId: string
  conversationId: string
  entryId: string
  onSaved: () => void
  onOpen: (document: DocumentLink) => void
}): React.JSX.Element {
  const [link, setLink] = React.useState<DocumentLink | null>(null)
  const [message, setMessage] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const sending = React.useRef(false)

  async function activate(): Promise<void> {
    if (sending.current) return
    if (link !== null) {
      onOpen(link)
      return
    }
    sending.current = true
    setBusy(true)
    setMessage('')
    try {
      const result = await promoteMonsterCard(campaignId, conversationId, entryId)
      if (result.kind !== 'ok') {
        setMessage(FAILURE_COPY[result.kind])
        return
      }
      setLink({
        document_id: result.document.document_id,
        title: String(result.document.data.name),
        type: 'statblock',
        library_category: 'bestiary',
      })
      setMessage('Saved to Bestiary.')
      onSaved()
    } finally {
      sending.current = false
      setBusy(false)
    }
  }

  return (
    <div className="assistant-lane__actions">
      <Button variant="text" size="small" aria-disabled={busy || undefined} onClick={() => void activate()}>
        {link !== null ? 'Open in Bestiary' : busy ? 'Saving…' : 'Save to Bestiary'}
      </Button>
      <span role="status">{message}</span>
    </div>
  )
}
