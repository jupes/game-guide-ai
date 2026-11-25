import * as React from 'react'
import { Button } from '../ds/Button'
import type { Seat } from '../gm/contracts'
import { getCharacterSheetLink, setCharacterSheetLink, type LinkReadResult } from '../gm/documentApi'
import { useLibraryList } from './useLibraryList'
import { useReveals } from './revealContext'

export function CharacterSheetLinks({
  campaignId, seats, fetchImpl,
}: {
  campaignId: string
  seats: readonly Seat[]
  fetchImpl: typeof fetch
}): React.JSX.Element {
  const sheets = useLibraryList({
    category: 'documents', type: 'character-sheet', archived: false, search: '', sort: 'name',
  }, fetchImpl)
  const [sheetId, setSheetId] = React.useState('')
  const items = sheets.state.status === 'ready'
    ? sheets.state.items.filter((item) => item.type === 'character-sheet' && !item.archived)
    : []
  const current = items.find((item) => item.document_id === sheetId)

  return (
    <div className="participants-panel__sheet-tools">
      {sheets.state.status === 'loading' ? (
        <p role="status">Loading character sheets…</p>
      ) : sheets.state.status === 'error' || sheets.state.status === 'invalid' ? (
        <>
          <p>Couldn't load character sheets.</p>
          <Button variant="text" onClick={sheets.retry}>Retry sheets</Button>
        </>
      ) : (
        <>
          {items.length === 0 ? <p>No character sheets yet.</p> : (
            <label>
              Character sheet
              <select className="library-panel__select" value={sheetId} onChange={(event) => setSheetId(event.target.value)}>
                <option value="">Choose a sheet</option>
                {items.map((item) => <option key={item.document_id} value={item.document_id}>{item.title}</option>)}
              </select>
            </label>
          )}
          {sheets.state.nextCursor !== null && (
            <Button variant="text" onClick={sheets.loadMore} disabled={sheets.state.more === 'loading'}>
              {sheets.state.more === 'failed' ? 'Retry more sheets' : 'Load more sheets'}
            </Button>
          )}
          {current !== undefined && (
            <LinkControls key={sheetId} campaignId={campaignId} documentId={sheetId} seats={seats} fetchImpl={fetchImpl} />
          )}
        </>
      )}
    </div>
  )
}

function LinkControls({
  campaignId, documentId, seats, fetchImpl,
}: {
  campaignId: string
  documentId: string
  seats: readonly Seat[]
  fetchImpl: typeof fetch
}): React.JSX.Element {
  const { refresh: refreshReveals } = useReveals()
  const [known, setKnown] = React.useState<LinkReadResult | null>(null)
  const [revision, readAgain] = React.useReducer((count: number) => count + 1, 0)
  const [seatId, setSeatId] = React.useState('')
  const [saving, setSaving] = React.useState(false)
  const [message, setMessage] = React.useState('')
  const savingNow = React.useRef(false)
  React.useEffect(() => {
    let current = true
    void getCharacterSheetLink(campaignId, documentId, fetchImpl).then((result) => {
      if (current) setKnown(result)
    })
    return () => { current = false }
  }, [campaignId, documentId, fetchImpl, revision])

  async function update(participantId: string | null): Promise<void> {
    if (savingNow.current) return
    savingNow.current = true
    setSaving(true)
    try {
      const result = await setCharacterSheetLink(campaignId, documentId, participantId, fetchImpl)
      if (participantId === null) void refreshReveals()
      if (result.kind === 'ok') setMessage(participantId === null ? 'Character sheet unlinked.' : 'Character sheet linked.')
      else if (result.kind === 'link_taken') setMessage('The sheet or participant already has a link.')
      else if (result.kind === 'throttled') setMessage('Please wait a moment before trying again.')
      else setMessage('Couldn’t change the character sheet link.')
      setKnown(null)
      readAgain()
    } finally {
      savingNow.current = false
      setSaving(false)
    }
  }

  const link = known?.kind === 'ok' ? known.link : null
  const linked = link?.participant_id ?? null
  const paired = linked === null ? null : seats.find((seat) => seat.participant_id === linked)?.alias
  return (
    <>
      {known === null ? <p>Checking the character sheet link…</p> : link === null ? (
        <>
          <p>Couldn't check the character sheet link.</p>
          <Button variant="text" onClick={() => { setKnown(null); readAgain() }}>Retry link</Button>
        </>
      ) : linked !== null ? (
        <>
          <p>Linked to {paired ?? 'a former participant'}.</p>
          <Button variant="outlined" disabled={saving} onClick={() => void update(null)}>Unlink sheet</Button>
        </>
      ) : seats.length === 0 ? (
        <p>Offer a seat before linking this sheet.</p>
      ) : (
        <>
          <label>
            Link to participant
            <select className="library-panel__select" value={seatId} onChange={(event) => setSeatId(event.target.value)}>
              <option value="">Choose a participant</option>
              {seats.map((seat) => <option key={seat.participant_id} value={seat.participant_id}>{seat.alias}</option>)}
            </select>
          </label>
          <Button variant="outlined" disabled={saving || !seats.some((seat) => seat.participant_id === seatId)}
            onClick={() => void update(seatId)}>Link sheet</Button>
        </>
      )}
      <p role="status">{message}</p>
    </>
  )
}
