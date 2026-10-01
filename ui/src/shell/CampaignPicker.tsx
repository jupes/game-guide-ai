/**
 * CampaignPicker -- choose a campaign or create the first one
 * (agent-forge-harness-1kg.2.5, PR-2; §12.2's Campaign picker row, brief
 * sections 7.1 and 11, the Critic's items 15 and 21).
 *
 * Built, not mounted: no screen renders it yet (`74j` does). It reads and
 * writes nothing but `useCampaign()`, so everything it shows is the server's
 * answer and nothing of it reaches web storage (SEC-49).
 *
 * - One live region (A-29): a `role="status"` node mounted empty with the
 *   picker, given one message when a load it started begins and one when that
 *   load ends, and one when a create settles (STATE-7). The list, the empty and
 *   error lines are visible text, never live regions.
 * - Retry and Load more stay mounted, `aria-disabled`, through their own
 *   request, so focus is never dropped; when one goes away while focused, focus
 *   moves to the heading (§11).
 * - Create is sent only by a press, never retried by the client (a create has
 *   no idempotency key), and the provider is single-flight besides.
 * - All local state -- the typed name and the last announcement included --
 *   belongs to one account: the picker is keyed as the provider is, by whether
 *   the account can use campaigns and its user id (critic 21; vtb9).
 */

import * as React from 'react'
import { Badge } from '../ds/Badge'
import { Button } from '../ds/Button'
import { TextField } from '../ds/TextField'
import type { Campaign } from '../gm/contracts'
import { useCampaign, type CampaignList } from './campaignContext'
import { CurrentUserContext } from './currentUser'
import '../ds/Button.css'
import './CampaignPicker.css'

// Copy (I-18): §12.2's strings verbatim; the rest are constants `cub` may replace.
const HEADING = 'Your campaigns'
const LOADING = 'Loading campaigns…'
const LOADED = 'Campaigns loaded'
const LOAD_FAILED = "Couldn't load campaigns"
const EMPTY = 'Create your first campaign — only a name is required'
const CREATED = 'Campaign created'
const CREATE_FAILED = "Couldn't create the campaign"
const NAME_INVALID = 'Give the campaign a name of 1 to 120 characters on one line.'
const BADGES: Record<NonNullable<Campaign['badge']>, string> = { live: 'LIVE', ready: 'READY' }

export interface PendingButtonProps {
  children: React.ReactNode
  /** Its own request is in flight: `aria-disabled`, and a press does nothing. */
  busy: boolean
  onPress: () => void
  /** Where focus goes if this button leaves the DOM while it has focus. */
  landing: React.RefObject<HTMLElement | null>
}

/** Read when focus must move, not when the effect ran: the landing may re-render in between. */
function focusOn(landing: React.RefObject<HTMLElement | null>): void {
  landing.current?.focus()
}

/** The DS text button, but `aria-disabled` rather than `disabled` while busy:
 * a disabled button drops focus (§11). */
export function PendingButton({ children, busy, onPress, landing }: PendingButtonProps): React.JSX.Element {
  const ref = React.useRef<HTMLButtonElement>(null)
  React.useLayoutEffect(() => {
    const button = ref.current
    // Runs while the button is still in the DOM: React unmounts effects first.
    return () => {
      if (button !== null && button.ownerDocument.activeElement === button) focusOn(landing)
    }
  }, [landing])
  return (
    <button
      ref={ref}
      type="button"
      className="aether-btn campaign-pending"
      data-variant="text"
      data-size="medium"
      data-touch-target="true"
      aria-disabled={busy || undefined}
      onClick={() => {
        if (!busy) onPress()
      }}
    >
      <span className="aether-btn__state" aria-hidden="true" />
      <span className="aether-btn__label">{children}</span>
    </button>
  )
}

/** A Retry shown while its read has `failed`, and -- once pressed -- through
 * the `busy` read it started, so it is never unmounted under the user's focus. */
// eslint-disable-next-line react-refresh/only-export-components -- the retry pattern is shared with LeftNav
export function useRetrying(failed: boolean, busy: boolean): readonly [shown: boolean, press: () => void] {
  const [pressed, setPressed] = React.useState(false)
  if (pressed && !failed && !busy) setPressed(false)
  return [failed || (pressed && busy), () => setPressed(true)]
}

function listAnnouncement(list: CampaignList): string {
  if (list.kind === 'loading' || (list.kind === 'ready' && list.loadingMore)) return LOADING
  if (list.kind === 'failed' || (list.kind === 'ready' && list.moreFailed)) return LOAD_FAILED
  return list.kind === 'ready' ? LOADED : ''
}

export interface CampaignPickerProps {
  /** A campaign was selected here, from the list or by a create. Focus is then the host's. */
  onSelected?: (campaign: Campaign) => void
}

export function CampaignPicker(props: CampaignPickerProps): React.JSX.Element {
  const userId = React.useContext(CurrentUserContext)?.user.id ?? 'guest'
  const { enabled } = useCampaign()
  return <Picker key={`${enabled}:${userId}`} {...props} />
}

function Picker({ onSelected }: CampaignPickerProps): React.JSX.Element {
  const { enabled, list, selection, loadCampaigns, loadMoreCampaigns, selectCampaign, createCampaign } = useCampaign()
  const heading = React.useRef<HTMLHeadingElement>(null)
  const nameField = React.useRef<HTMLInputElement | HTMLTextAreaElement>(null)
  const ids = React.useId()
  const errorId = `${ids}-error`
  const [name, setName] = React.useState('')
  const [creating, setCreating] = React.useState(false)
  const [nameError, setNameError] = React.useState<string | null>(null)
  // What the status node follows: loads this picker started, or its last create.
  const [track, setTrack] = React.useState<'list' | 'create' | null>(list.kind === 'idle' ? 'list' : null)
  const [createNote, setCreateNote] = React.useState('')
  const [retryShown, pressRetry] = useRetrying(list.kind === 'failed', list.kind === 'loading')

  // `enabled` too: the same user id losing and regaining campaigns keeps the
  // list 'idle' across the reset, and the new account must still be read.
  React.useEffect(() => {
    if (enabled && list.kind === 'idle') loadCampaigns()
  }, [enabled, list.kind, loadCampaigns])

  const items = list.kind === 'idle' ? [] : list.items
  const announcement = track === 'create' ? createNote : track === 'list' ? listAnnouncement(list) : ''

  const choose = (campaign: Campaign): void => {
    void selectCampaign(campaign).then((outcome) => {
      if (outcome === 'switched' || outcome === 'unchanged') onSelected?.(campaign)
    })
  }

  const create = (event: React.FormEvent): void => {
    event.preventDefault()
    if (creating) return
    setCreating(true)
    // From the press on, the announcer is the create's: the list re-read a
    // failed create starts in the background is not announced.
    setTrack('create')
    setCreateNote('')
    void createCampaign(name).then((outcome) => {
      setCreating(false)
      if (outcome.kind === 'vetoed') return
      if (outcome.kind === 'created') {
        setName('')
        setNameError(null)
        setCreateNote(CREATED)
        onSelected?.(outcome.campaign)
        return
      }
      // Announced as well as described: focus already on the field (an Enter)
      // moves nowhere, so nothing would re-read the new description.
      const problem = outcome.kind === 'invalid' ? NAME_INVALID : CREATE_FAILED
      setNameError(problem)
      setCreateNote(problem)
      nameField.current?.focus()
    })
  }

  return (
    <section className="campaign-picker" aria-labelledby={`${ids}-heading`}>
      <h2 id={`${ids}-heading`} ref={heading} tabIndex={-1} className="campaign-picker__heading">{HEADING}</h2>
      <p role="status" className="campaign-picker__status">{announcement}</p>

      {items.length > 0 && (
        <ul className="campaign-picker__list" aria-label={HEADING}>
          {items.map((campaign) => (
            <li key={campaign.campaign_id}>
              <button
                type="button"
                className="campaign-picker__item"
                aria-pressed={selection.kind === 'selected' && selection.campaign.campaign_id === campaign.campaign_id}
                onClick={() => choose(campaign)}
              >
                <span className="campaign-picker__name">{campaign.name}</span>
                {campaign.badge !== null && <>{' '}<Badge tone="primary">{BADGES[campaign.badge]}</Badge></>}
                {campaign.concluded_at !== null && <>{' '}<Badge tone="neutral">Concluded</Badge></>}
              </button>
            </li>
          ))}
        </ul>
      )}

      {list.kind === 'loading' && items.length === 0 && (
        <>
          <ul className="campaign-picker__list" aria-hidden="true">
            <li className="campaign-picker__skeleton" />
            <li className="campaign-picker__skeleton" />
          </ul>
          <p className="campaign-picker__message">{LOADING}</p>
        </>
      )}
      {list.kind === 'ready' && items.length === 0 && <p className="campaign-picker__message">{EMPTY}</p>}
      {retryShown && (
        <div className="campaign-picker__row">
          <p className="campaign-picker__message">{LOAD_FAILED}</p>
          <PendingButton busy={list.kind === 'loading'} landing={heading} onPress={() => {
            pressRetry()
            setTrack('list')
            loadCampaigns()
          }}>Retry</PendingButton>
        </div>
      )}
      {list.kind === 'ready' && list.nextCursor !== null && (
        <div className="campaign-picker__row">
          {list.moreFailed && <p className="campaign-picker__message">{LOAD_FAILED}</p>}
          <PendingButton busy={list.loadingMore} landing={heading} onPress={() => {
            setTrack('list')
            loadMoreCampaigns()
          }}>Load more</PendingButton>
        </div>
      )}

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
    </section>
  )
}

