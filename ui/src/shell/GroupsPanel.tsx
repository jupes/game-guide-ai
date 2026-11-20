import * as React from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../ds/Button'
import { TextField } from '../ds/TextField'
import type { Group, Seat } from '../gm/contracts'
import { readGroups, writeGroup, type GroupChange } from './groupsApi'
import { LibraryDialog } from './LibraryDialog'
import { useReveals } from './revealContext'

type GroupAction =
  | { kind: 'create' }
  | { kind: 'rename'; group: Group }
  | { kind: 'remove'; group: Group }
  | { kind: 'add-member'; group: Group }
  | { kind: 'remove-member'; group: Group; seat: Seat }

const ERROR_COPY = {
  invalid: 'Enter a group name of 1 to 40 characters.',
  duplicate: 'A group already uses that name.',
  unavailable: 'This group or participant is unavailable.',
  failed: 'Couldn’t save the change. Try again.',
  limit: 'This campaign already has the maximum number of groups.',
  throttled: 'Please wait a moment before trying again.',
} as const

export function GroupsPanel({
  campaignId, seats, fetchImpl, onDialogOpenChange,
}: {
  campaignId: string
  seats: readonly Seat[]
  fetchImpl: typeof fetch
  onDialogOpenChange: (open: boolean) => void
}): React.JSX.Element {
  const [groups, setGroups] = React.useState<readonly Group[] | null>()
  const [revision, reload] = React.useReducer((current: number) => current + 1, 0)
  const [action, setAction] = React.useState<GroupAction | null>(null)
  const [notice, setNotice] = React.useState('')
  const opener = React.useRef<HTMLElement | null>(null)
  const heading = React.useRef<HTMLHeadingElement>(null)
  const { refresh: refreshReveals } = useReveals()

  React.useEffect(() => {
    let current = true
    void readGroups(campaignId, fetchImpl).then((rows) => {
      if (current) setGroups(rows)
    })
    return () => { current = false }
  }, [campaignId, fetchImpl, revision])
  React.useEffect(() => () => onDialogOpenChange(false), [onDialogOpenChange])

  const returnFocus = React.useCallback(() => {
    if (opener.current?.isConnected) opener.current.focus()
    if (document.activeElement !== opener.current) heading.current?.focus()
  }, [])

  function open(next: GroupAction, button: HTMLElement): void {
    opener.current = button
    setAction(next)
    onDialogOpenChange(true)
  }

  function close(): void {
    setAction(null)
    onDialogOpenChange(false)
  }

  function saved(change: GroupChange, updated: Group | null): void {
    setGroups((rows) => {
      if (!rows) return rows
      if (change.kind === 'create') return updated && !rows.some((row) => row.group_id === updated.group_id)
        ? [...rows, updated] : rows
      if (change.kind === 'remove') return rows.filter((row) => row.group_id !== change.groupId)
      return rows.map((row) => {
        if (row.group_id !== change.groupId) return row
        if (change.kind === 'rename') return updated ?? row
        const member_ids = change.kind === 'add-member'
          ? [...new Set([...row.member_ids, change.seatId])].sort()
          : row.member_ids.filter((id) => id !== change.seatId)
        return { ...row, member_ids }
      })
    })
    setNotice(change.kind === 'remove' ? 'Group removed.' : change.kind === 'remove-member'
      ? 'Participant removed from the group.' : change.kind === 'add-member' ? 'Participant added to the group.' : 'Group saved.')
    close()
    reload()
    void refreshReveals()
  }

  return (
    <div className="participants-panel__sheet-tools">
      <h3 ref={heading} tabIndex={-1}>Named groups</h3>
      {groups === undefined ? <p role="status">Loading groups…</p> : groups === null ? (
        <>
          <p>Couldn't load groups.</p>
          <Button variant="text" onClick={() => { setGroups(undefined); reload() }}>Retry groups</Button>
        </>
      ) : (
        <>
          <Button variant="filled" onClick={(event) => open({ kind: 'create' }, event.currentTarget)}>New group</Button>
          {groups.length === 0 && <p>No groups yet.</p>}
          <ul className="participants-panel__seats" aria-label="Campaign groups">
            {groups.map((group) => {
              const members = seats.filter((seat) => group.member_ids.includes(seat.participant_id))
              return (
                <li key={group.group_id}>
                  <h4>{group.name}</h4>
                  <div className="participants-panel__actions">
                    <Button variant="text" aria-label={`Rename group ${group.name}`}
                      onClick={(event) => open({ kind: 'rename', group }, event.currentTarget)}>Rename group</Button>
                    <Button variant="text" aria-label={`Remove group ${group.name}`}
                      onClick={(event) => open({ kind: 'remove', group }, event.currentTarget)}>Remove group</Button>
                    {seats.some((seat) => !group.member_ids.includes(seat.participant_id)) && (
                      <Button variant="text" aria-label={`Add member to ${group.name}`}
                        onClick={(event) => open({ kind: 'add-member', group }, event.currentTarget)}>Add member</Button>
                    )}
                  </div>
                  {members.length === 0 ? <p>No members yet.</p> : (
                    <ul aria-label={`Members of ${group.name}`} className="participants-panel__members">
                      {members.map((seat) => (
                        <li key={seat.participant_id}>
                          <span>{seat.alias}{seat.status !== 'confirmed' && ' (unconfirmed)'}</span>
                          <Button variant="text" aria-label={`Remove ${seat.alias} from ${group.name}`}
                            onClick={(event) => open({ kind: 'remove-member', group, seat }, event.currentTarget)}>
                            Remove
                          </Button>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              )
            })}
          </ul>
        </>
      )}
      <p role="status" className="library-panel__sr-only">{notice}</p>
      {action !== null && createPortal(
        <GroupDialog
          action={action}
          campaignId={campaignId}
          seats={seats}
          fetchImpl={fetchImpl}
          onSave={saved}
          onCancel={close}
          returnFocus={returnFocus}
        />,
        document.body,
      )}
    </div>
  )
}

function GroupDialog({
  action, campaignId, seats, fetchImpl, onSave, onCancel, returnFocus,
}: {
  action: GroupAction
  campaignId: string
  seats: readonly Seat[]
  fetchImpl: typeof fetch
  onSave: (change: GroupChange, group: Group | null) => void
  onCancel: () => void
  returnFocus: () => void
}): React.JSX.Element {
  const [name, setName] = React.useState(action.kind === 'rename' ? action.group.name : '')
  const [seatId, setSeatId] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [commandId] = React.useState(() => crypto.randomUUID())
  const busyRef = React.useRef(false)
  const nameRef = React.useRef<HTMLInputElement | HTMLTextAreaElement>(null)
  const seatRef = React.useRef<HTMLSelectElement>(null)
  const naming = action.kind === 'rename' || action.kind === 'create'
  const deleting = action.kind === 'remove' || action.kind === 'remove-member'
  const remaining = action.kind === 'add-member'
    ? seats.filter((seat) => !action.group.member_ids.includes(seat.participant_id)) : []

  const heading = action.kind === 'create' ? 'New group'
    : action.kind === 'rename' ? `Rename ${action.group.name}`
      : action.kind === 'remove' ? `Remove ${action.group.name}`
        : action.kind === 'add-member' ? `Add a member to ${action.group.name}`
          : `Remove ${action.seat.alias} from ${action.group.name}`
  async function submit(): Promise<void> {
    if (busyRef.current) return
    let change: GroupChange
    switch (action.kind) {
      case 'create':
        change = { kind: 'create', name, commandId }
        break
      case 'rename':
        change = { kind: 'rename', name, groupId: action.group.group_id }
        break
      case 'remove':
        change = { kind: 'remove', groupId: action.group.group_id }
        break
      case 'add-member':
        if (!remaining.some((seat) => seat.participant_id === seatId)) {
          setError('Choose a participant.')
          return
        }
        change = { kind: 'add-member', groupId: action.group.group_id, seatId }
        break
      case 'remove-member':
        change = { kind: 'remove-member', groupId: action.group.group_id, seatId: action.seat.participant_id }
        break
    }
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await writeGroup(campaignId, change, fetchImpl)
      if (result.kind === 'ok') onSave(change, result.group)
      else setError(ERROR_COPY[result.kind])
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  return (
    <LibraryDialog
      heading={heading}
      body={action.kind === 'remove' ? 'Removing the group may clear live private views shared through it.'
        : deleting ? 'Removing this participant may clear live private views shared through the group.' : undefined}
      confirmLabel={busy ? 'Saving…' : deleting ? 'Remove' : naming ? 'Save group' : 'Add member'}
      cancelLabel="Cancel"
      busy={busy}
      destructive={deleting}
      initialFocus={deleting ? 'cancel' : 'field'}
      fieldRef={naming ? nameRef : seatRef}
      error={error}
      onConfirm={() => void submit()}
      onCancel={onCancel}
      returnFocus={returnFocus}
    >
      {naming && <TextField label="Group name" ref={nameRef} value={name}
        onChange={(event) => setName(event.target.value)} disabled={busy} fullWidth />}
      {action.kind === 'add-member' && (
        <label>
          Participant
          <select className="library-panel__select" ref={seatRef} value={seatId}
            onChange={(event) => setSeatId(event.target.value)} disabled={busy}>
            <option value="">Choose a participant</option>
            {remaining.map((seat) => <option key={seat.participant_id} value={seat.participant_id}>{seat.alias}</option>)}
          </select>
        </label>
      )}
    </LibraryDialog>
  )
}
