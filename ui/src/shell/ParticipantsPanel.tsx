import * as React from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../ds/Button'
import { useCanvasState } from './canvasContext'
import { useCampaign } from './campaignContext'
import { listCampaignSeats } from './campaignApi'
import type { Seat, SeatStatus } from '../gm/contracts'
import { PARTICIPANTS_PANEL_ID, useLibraryPanel } from './libraryPanel'
import { ParticipantSeatDialog, type SeatAction, type SeatChange } from './ParticipantSeatDialog'
import { CharacterSheetLinks } from './CharacterSheetLinks'
import { useReveals } from './revealContext'
import type { ShellLayout } from './breakpoints'
import './CampaignLibraryPanel.css'
import './ParticipantsPanel.css'

const SEAT_LABEL: Record<SeatStatus, string> = {
  open: 'Open seat',
  offered: 'Offer pending',
  not_accepted: 'Offer expired or not accepted',
  awaiting_confirmation: 'Awaiting your confirmation',
  confirmed: 'Seated',
  removed: 'Removed',
}

export function ParticipantsPanel({
  layout, inert, onDialogOpenChange,
}: {
  layout: ShellLayout
  inert?: boolean
  onDialogOpenChange: (open: boolean) => void
}): React.JSX.Element | null {
  const { scope } = useCampaign()
  return scope === null ? null : (
    <ParticipantsPanelBody
      key={scope.key}
      campaignId={scope.campaignId}
      layout={layout}
      inert={inert === true}
      onDialogOpenChange={onDialogOpenChange}
    />
  )
}

function ParticipantsPanelBody({
  campaignId, layout, inert, onDialogOpenChange,
}: {
  campaignId: string
  layout: ShellLayout
  inert: boolean
  onDialogOpenChange: (open: boolean) => void
}): React.JSX.Element {
  const { closeLibrary } = useLibraryPanel()
  const { guardDialog } = useCanvasState()
  const { refresh: refreshReveals } = useReveals()
  const headingId = React.useId()
  const heading = React.useRef<HTMLHeadingElement>(null)
  const opener = React.useRef<HTMLElement | null>(null)
  const [seats, setSeats] = React.useState<readonly Seat[] | null>(null)
  const [failed, setFailed] = React.useState(false)
  const [revision, reload] = React.useReducer((count: number) => count + 1, 0)
  const [selected, setSelected] = React.useState<SeatAction | null>(null)
  const [showSheets, setShowSheets] = React.useState(false)
  const [announcement, setAnnouncement] = React.useState('')
  const lifetime = React.useRef<AbortController | null>(null)

  React.useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller
    heading.current?.focus()
    return () => {
      controller.abort()
      onDialogOpenChange(false)
    }
  }, [onDialogOpenChange])

  const fetchForPanel = React.useCallback<typeof fetch>((input, init) => {
    const controller = lifetime.current
    return controller === null || controller.signal.aborted
      ? Promise.reject(new DOMException('Aborted', 'AbortError'))
      : fetch(input, { ...init, signal: controller.signal })
  }, [])

  React.useEffect(() => {
    let obsolete = false
    void listCampaignSeats(campaignId, fetchForPanel).then((result) => {
      if (obsolete) return
      setFailed(result.kind !== 'ok')
      if (result.kind === 'ok') setSeats(result.items.filter((seat) => seat.status !== 'removed'))
    })
    return () => { obsolete = true }
  }, [campaignId, fetchForPanel, revision])

  const close = React.useCallback(() => closeLibrary({ returnFocus: true }), [closeLibrary])
  React.useEffect(() => {
    if (inert || guardDialog !== null || selected !== null) return
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      close()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [close, guardDialog, inert, selected])

  function open(action: SeatAction, button: HTMLElement): void {
    opener.current = button
    setSelected(action)
    onDialogOpenChange(true)
  }

  const returnFocus = React.useCallback(() => {
    const button = opener.current
    if (button?.isConnected) button.focus()
    if (document.activeElement !== button) heading.current?.focus()
  }, [])
  const finish = (change?: SeatChange) => {
    setSelected(null)
    onDialogOpenChange(false)
    if (change !== undefined) {
      setAnnouncement(change.message)
      if (change.removedId !== undefined) {
        setSeats((current) => current?.filter((seat) => seat.participant_id !== change.removedId) ?? null)
        void refreshReveals()
      }
      const confirmed = change.confirmed
      if (confirmed !== undefined) {
        setSeats((current) => current?.map((seat) => seat.participant_id === confirmed.participant_id ? confirmed : seat) ?? null)
        void refreshReveals()
      }
    }
    reload()
  }

  return (
    <section
      id={PARTICIPANTS_PANEL_ID}
      className="library-panel"
      aria-labelledby={headingId}
      data-layout={layout}
      inert={inert || undefined}
    >
      <div className="library-panel__head">
        {layout === 'narrow' && (
          <button type="button" className="library-panel__back" onClick={close}>
            <span className="material-symbols-rounded" aria-hidden="true">arrow_back</span>
            Back
          </button>
        )}
        <h2 id={headingId} ref={heading} className="library-panel__heading" tabIndex={-1}>Participants</h2>
        {layout !== 'narrow' && (
          <button type="button" className="library-panel__close" aria-label="Close participants" onClick={close}>
            <span className="material-symbols-rounded" aria-hidden="true">close</span>
          </button>
        )}
      </div>
      <div className="participants-panel__content">
        {failed ? (
          <div className="library-panel__problem">
            <p>Couldn't load participants.</p>
            <Button variant="outlined" onClick={() => { setFailed(false); reload() }}>Retry</Button>
          </div>
        ) : seats === null ? (
          <div className="library-panel__loading">
            <div className="library-panel__skeleton" aria-hidden="true" />
            <div className="library-panel__skeleton" aria-hidden="true" />
            <p role="status">Loading participants…</p>
          </div>
        ) : (
          <>
            <div className="participants-panel__actions">
              <Button variant="filled" icon="person_add" onClick={(event) => open({ kind: 'add' }, event.currentTarget)}>
                Offer a seat
              </Button>
              <Button variant="text" onClick={reload}>Refresh</Button>
            </div>
            {seats.length === 0 ? (
              <p>No participants yet. Add the players who need a private view.</p>
            ) : (
              <ul className="participants-panel__seats" aria-label="Campaign seats">
                {seats.map((seat) => (
                  <li key={seat.participant_id}>
                    <h3>{seat.alias}</h3>
                    <p>{SEAT_LABEL[seat.status]}</p>
                    {seat.address !== null && <p className="participants-panel__address">{seat.address}</p>}
                    <div className="participants-panel__actions">
                      {(seat.status === 'open' || seat.status === 'not_accepted') && (
                        <Button variant="text" aria-label={`Offer seat for ${seat.alias}`}
                          onClick={(event) => open({ kind: 'offer', seat }, event.currentTarget)}>Offer seat</Button>
                      )}
                      {seat.status === 'awaiting_confirmation' && (
                        <Button variant="text" aria-label={`Confirm ${seat.alias}`}
                          onClick={(event) => open({ kind: 'confirm', seat }, event.currentTarget)}>Confirm</Button>
                      )}
                      <Button variant="text" aria-label={`Remove ${seat.alias}`}
                        onClick={(event) => open({ kind: 'remove', seat }, event.currentTarget)}>Remove</Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            <Button variant="outlined" aria-expanded={showSheets} onClick={() => setShowSheets((open) => !open)}>
              Character sheets
            </Button>
            {showSheets && <CharacterSheetLinks campaignId={campaignId} seats={seats} fetchImpl={fetchForPanel} />}
          </>
        )}
        <p role="status" className="library-panel__sr-only">{announcement}</p>
      </div>
      {selected !== null && createPortal(
        <ParticipantSeatDialog
          campaignId={campaignId}
          action={selected}
          fetchImpl={fetchForPanel}
          returnFocus={returnFocus}
          onClose={finish}
        />,
        document.body,
      )}
    </section>
  )
}
