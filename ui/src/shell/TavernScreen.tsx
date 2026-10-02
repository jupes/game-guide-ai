/**
 * TavernScreen -- "Your Campaigns" at /tavern (agent-forge-harness-74j routed
 * it; 30c PR-1 builds its body).
 *
 * The GM's campaigns as cards, newest activity first, concluded ones folded
 * into a disclosure at the foot, and a Begin anew card that creates one. Every
 * signed-in account reaches it (PR-2): an account that can use campaigns
 * (`useCampaign().enabled`, today a dm) gets all of that, and every account
 * gets "Your seats" -- the tables where it holds a seat, read from `GET /seats`.
 * A player with no seat gets the spec's E-1 panel; the header's New Campaign is
 * then shown locked, with its reason, never hidden. It reads nothing from the
 * URL, writes nothing to web storage or the page title, and has exactly one live
 * region: the status node, mounted empty with the list and given the messages in
 * the spec's section 3.4 (one at the start of a read, one at its end).
 *
 * - Seats (ID-17, ID-21 to ID-28): informational cards with nothing to press. A
 *   confirmed seat whose table is live reads "Live now" as plain text, never a
 *   link, because no table page exists yet (`1kg.7.4`, SEC-43). An unconfirmed
 *   seat reads "Waiting for your GM to confirm your seat".
 * - One Retry serves both reads: it re-asks whichever of campaigns and seats failed.
 *
 * - Read once per visit, every page (the order is the client's, and
 *   "Concluded (N)" counts them all), with a hard ceiling of
 *   `TAVERN_MAX_PAGES` against a runaway cursor; past it, "Load more".
 * - Focus: an in-app arrival focuses the heading, then -- once the read is
 *   complete and only if focus is still on the heading -- the top card's Prep.
 *   A cold load moves no focus. "Show more" focuses the first card it reveals;
 *   Mark concluded focuses the Concluded toggle and Reopen the reopened card's
 *   Prep; a failure leaves focus on the pending button.
 * - Prep selects the campaign and opens its GM channel (74j R-1). Start Session
 *   is shown locked and does nothing (ID-6).
 * - The account decides everything: the list section is keyed on the account,
 *   so per-card busy state, the announcement and the paging reset with it, and
 *   a conclude answer for a previous account is dropped by the store's epoch.
 */
import * as React from 'react'
import { Button } from '../ds/Button'
import type { Campaign } from '../gm/contracts'
import { useAppNav, type ChatMode } from './AppNav'
import { BeginAnewCard } from './BeginAnewCard'
import { EMPTY, LOADED, LOADING, LOAD_FAILED, PendingButton, useRetrying } from './CampaignPicker'
import { useCampaign } from './campaignContext'
import { useConversationStore } from './ConversationStoreContext'
import { useCurrentUser } from './currentUser'
import { MODES } from './modes'
import { TavernActionButton } from './TavernActionButton'
import { TavernCampaignCard } from './TavernCampaignCard'
import { TavernNoSeats } from './TavernNoSeats'
import { TavernSeatCard } from './TavernSeatCard'
import { TAVERN_MAX_PAGES, TAVERN_PAGE_SIZE, latestConversation, orderCampaigns, orderSeats } from './tavernOrder'
import { useTavernSeats } from './useTavernSeats'
import './TavernScreen.css'

// Copy (74j I-12): constants the design lane (`cub`, `30c`) may replace.
const HEADING = 'Your Campaigns'
const SUBHEAD = 'Pick up where the story left off, or begin anew.'
const SEATS_SUBHEAD = 'The tables where you have a seat.'
const NEW_CAMPAIGN = 'New Campaign'
const NEW_CAMPAIGN_LOCKED = 'Creating campaigns is not available to your account yet'
const SEATS_HEADING = 'Your seats'
const SEATS_LOADING = 'Loading your seats…'
const SEATS_LOADED = 'Seats loaded'
const SEATS_FAILED = "Couldn't load your seats"
const SHOW_MORE_SEATS = 'Show more seats'
const CONTINUE_WITHOUT = 'Continue without a campaign'
const BACK_TO_CHAT = 'Back to chat'
const SHOW_MORE = 'Show more campaigns'
const SHOW_MORE_CONCLUDED = 'Show more concluded campaigns'
const WHERE_YOU_WERE = 'Where you were'
const BACK_TO_THREAD = 'Back to that thread'
const CONCLUDING = 'Marking concluded…'
const CONCLUDED_NOTE = 'Marked concluded'
const REOPENING = 'Reopening…'
const REOPENED = 'Reopened'
const CHANGE_FAILED = "Couldn't change the campaign. Try again."
const UNAVAILABLE = 'That campaign is no longer available'

export function TavernScreen(): React.JSX.Element | null {
  const { enabled, selection, loadCampaigns, clearCampaign } = useCampaign()
  const { navIntent, mode, setMode, setConversationId, enterWorkspace, backToWorkspace } = useAppNav()
  const { user } = useCurrentUser()
  const lockedReasonId = React.useId()
  const heading = React.useRef<HTMLHeadingElement>(null)
  const nameField = React.useRef<HTMLInputElement | HTMLTextAreaElement>(null)
  // What a continuation checks when it resolves: this screen is still
  // mounted, for the same account (74j critic 5).
  const live = React.useRef({ mounted: false, userId: user.id })
  const [arrivedInApp] = React.useState(navIntent === 'push')

  // 74j critic 6: an account that cannot use campaigns is never in the GM
  // channel. It no longer leaves the screen (30c PR-2, ID-22): the tavern is
  // every signed-in account's, and a player's holds its seats.
  React.useLayoutEffect(() => {
    if (!enabled && mode === 'gm') setMode('sage')
  }, [enabled, mode, setMode])

  React.useLayoutEffect(() => {
    live.current.userId = user.id
  })
  React.useEffect(() => {
    const state = live.current
    state.mounted = true
    return () => {
      state.mounted = false
    }
  }, [])

  // 74j I-9: one list read per visit. The list section reads from `idle`
  // too (a fresh account), and the provider ignores a call while a read is in flight.
  React.useEffect(() => {
    if (enabled) loadCampaigns()
  }, [enabled, loadCampaigns])

  // 74j I-10: an in-app arrival focuses the page heading; a cold load moves nothing.
  React.useLayoutEffect(() => {
    if (arrivedInApp) heading.current?.focus()
  }, [arrivedInApp])

  const pressedAs = user.id
  const stillHere = (): boolean => live.current.mounted && live.current.userId === pressedAs

  // 74j R-1: a pick or a create made here always closes the open
  // conversation, then opens the campaign's GM channel.
  const onSelected = (): void => {
    if (!stillHere()) return
    setConversationId(null)
    enterWorkspace('gm')
  }
  const continueWithout = (): void => {
    void clearCampaign().then((outcome) => {
      if (!stillHere() || (outcome !== 'switched' && outcome !== 'unchanged')) return
      setConversationId(null)
      enterWorkspace('gm')
    })
  }
  const openThread = (id: string, threadMode: ChatMode): void => {
    if (!stillHere()) return
    enterWorkspace(threadMode)
    setConversationId(id)
  }
  const askTheSage = (): void => {
    if (!stillHere()) return
    enterWorkspace('sage')
  }

  return (
    <main className="tavern-screen">
      <div className="tavern-screen__column">
        <header className="tavern-screen__header">
          <h1 ref={heading} tabIndex={-1} className="tavern-screen__heading">{HEADING}</h1>
          <p className="tavern-screen__subhead">{enabled ? SUBHEAD : SEATS_SUBHEAD}</p>
          <div className="tavern-screen__actions">
            {enabled ? (
              <Button variant="filled" icon="add" onClick={() => nameField.current?.focus()}>{NEW_CAMPAIGN}</Button>
            ) : (
              <>
                <TavernActionButton variant="filled" icon="add" ariaDisabled ariaDescribedBy={lockedReasonId} onPress={() => {}}>
                  {NEW_CAMPAIGN}
                </TavernActionButton>
                <span id={lockedReasonId} className="tavern__sr-only">{NEW_CAMPAIGN_LOCKED}</span>
              </>
            )}
            {selection.kind !== 'none' && (
              <Button variant="outlined" onClick={continueWithout}>{CONTINUE_WITHOUT}</Button>
            )}
            <Button variant="outlined" icon="arrow_back" onClick={backToWorkspace}>{BACK_TO_CHAT}</Button>
          </div>
        </header>
        <TavernCampaigns
          key={`${enabled}:${user.id}`}
          heading={heading}
          nameField={nameField}
          arrivedInApp={arrivedInApp}
          stillHere={stillHere}
          onSelected={onSelected}
          openThread={openThread}
          askTheSage={askTheSage}
        />
      </div>
    </main>
  )
}

interface TavernCampaignsProps {
  heading: React.RefObject<HTMLHeadingElement | null>
  nameField: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>
  arrivedInApp: boolean
  stillHere: () => boolean
  onSelected: () => void
  openThread: (id: string, mode: ChatMode) => void
  askTheSage: () => void
}

/** What a commit should focus once the store's answer has rendered. */
type FocusTarget = { readonly kind: 'toggle' } | { readonly kind: 'prep'; readonly id: string }

function TavernCampaigns({
  heading, nameField, arrivedInApp, stillHere, onSelected, openThread, askTheSage,
}: TavernCampaignsProps): React.JSX.Element {
  const { enabled, list, loadCampaigns, loadMoreCampaigns, selectCampaign, setConcluded, readSeats } = useCampaign()
  const store = useConversationStore()
  // Declared before the effects below, so a visit's seat read is issued first.
  const seats = useTavernSeats(readSeats)
  const ids = React.useId()
  const regionId = `${ids}-concluded`
  const [now] = React.useState(() => new Date())
  const [shownSeats, setShownSeats] = React.useState(TAVERN_PAGE_SIZE)
  const [retrying, setRetrying] = React.useState({ campaigns: false, seats: false })
  // Pages read this visit: the first read is page 1, and each page that starts
  // after it adds one (counted from the render that sees its read begin).
  const [pages, setPages] = React.useState(0)
  const [wasLoadingMore, setWasLoadingMore] = React.useState(false)
  const [shown, setShown] = React.useState(TAVERN_PAGE_SIZE)
  const [shownConcluded, setShownConcluded] = React.useState(TAVERN_PAGE_SIZE)
  const [open, setOpen] = React.useState(false)
  const [busy, setBusy] = React.useState<ReadonlySet<string>>(new Set())
  // What the status node follows: this visit's reads, or the last note
  // (a create, a conclude or a reopen). Armed by the visit's first read, so the
  // node is empty at mount even on a revisit.
  const [armed, setArmed] = React.useState(false)
  const [track, setTrack] = React.useState<'list' | 'note'>('list')
  const [note, setNote] = React.useState('')
  const prepNodes = React.useRef(new Map<string, HTMLButtonElement>())
  const toggle = React.useRef<HTMLButtonElement>(null)
  const [target, setTarget] = React.useState<FocusTarget | null>(null)
  const focusedTarget = React.useRef<FocusTarget | null>(null)
  const autoFocused = React.useRef(false)
  const items = React.useMemo(() => (list.kind === 'idle' ? [] : list.items), [list])
  const { active, concluded } = React.useMemo(() => orderCampaigns(items), [items])
  const loadingMore = list.kind === 'ready' && list.loadingMore

  if (!armed && list.kind === 'loading') setArmed(true)
  if (list.kind === 'loading' && pages !== 1) setPages(1)
  if (loadingMore !== wasLoadingMore) {
    setWasLoadingMore(loadingMore)
    if (loadingMore) setPages(pages + 1)
  }

  // A fresh account's list is idle across the remount: it still has to be read.
  React.useEffect(() => {
    if (enabled && list.kind === 'idle') loadCampaigns()
  }, [enabled, list.kind, loadCampaigns])

  // ID-11: follow the cursor until the list is whole, or the ceiling.
  React.useEffect(() => {
    if (list.kind !== 'ready' || pages === 0) return
    if (list.nextCursor === null || list.loadingMore || list.moreFailed || pages >= TAVERN_MAX_PAGES) return
    loadMoreCampaigns()
  }, [list, pages, loadMoreCampaigns])

  const campaignsFailed = list.kind === 'failed' || (list.kind === 'ready' && list.moreFailed)
  const campaignsReading = list.kind === 'loading' || (list.kind === 'ready' && list.loadingMore)
  // Only a read this visit started counts: a revisit's stale list is not complete.
  const complete = armed && list.kind === 'ready' && !list.loadingMore
    && (list.nextCursor === null || pages >= TAVERN_MAX_PAGES || list.moreFailed)
  // One Retry for both reads (ID-21). Which of them it was pressed for is kept
  // while it runs, so each read's failure line stays up until its own read answers.
  const [retryShown, pressRetry] = useRetrying(campaignsFailed || seats.failed, campaignsReading || seats.reading)
  if (!retryShown && (retrying.campaigns || retrying.seats)) setRetrying({ campaigns: false, seats: false })
  const campaignsLine = campaignsFailed || (retryShown && retrying.campaigns)
  const seatsLine = seats.failed || (retryShown && retrying.seats)
  // Past the ceiling with a cursor left: Load more reads one page a press, and
  // stays mounted through that press (pages then exceeds the ceiling).
  const atCeiling = list.kind === 'ready' && list.nextCursor !== null && !list.moreFailed
    && pages >= TAVERN_MAX_PAGES && (pages > TAVERN_MAX_PAGES || !list.loadingMore)

  // A campaign account's start is its campaign read and its end waits for the
  // seat read too (one message at each end); a player's is the seat read alone.
  const campaignNote = !armed ? '' : campaignsFailed ? LOAD_FAILED
    : complete ? (seats.failed ? SEATS_FAILED : seats.complete ? LOADED : LOADING) : LOADING
  const seatNote = !seats.started ? '' : seats.failed ? SEATS_FAILED : seats.complete ? SEATS_LOADED : SEATS_LOADING
  const listNote = enabled ? campaignNote : seatNote
  const announcement = track === 'note' ? note : listNote

  const announce = (text: string): void => {
    setTrack('note')
    setNote(text)
  }

  // ID-4: once the read is complete, an in-app arrival that is still on the
  // heading (or nowhere) moves to the top card's Prep.
  React.useEffect(() => {
    if (autoFocused.current || !complete) return
    autoFocused.current = true
    if (!arrivedInApp || active.length === 0) return
    const at = document.activeElement
    if (at !== heading.current && at !== document.body) return
    prepNodes.current.get(active[0].campaign_id)?.focus()
  }, [complete, active, arrivedInApp, heading])

  // A card past the shown ones is revealed (its page of 12) when a press asks to focus it.
  const targetIndex = target?.kind === 'prep' ? active.findIndex((c) => c.campaign_id === target.id) : -1
  const visibleActive = targetIndex < 0 ? shown : Math.max(shown, Math.ceil((targetIndex + 1) / TAVERN_PAGE_SIZE) * TAVERN_PAGE_SIZE)

  // The moves a press asks for, made after the commit that renders its answer
  // (a card or the toggle that is not there yet waits for the commit that has it).
  React.useEffect(() => {
    if (target === null || focusedTarget.current === target) return
    const node = target.kind === 'toggle' ? toggle.current : prepNodes.current.get(target.id)
    if (node === null || node === undefined) return
    focusedTarget.current = target
    node.focus()
  })

  const prepRef = (id: string) => (node: HTMLButtonElement | null): void => {
    if (node === null) prepNodes.current.delete(id)
    else prepNodes.current.set(id, node)
  }

  const prep = (campaign: Campaign): void => {
    void selectCampaign(campaign).then((outcome) => {
      if (outcome === 'switched' || outcome === 'unchanged') onSelected()
    })
  }

  const change = (campaign: Campaign, concludedNow: boolean): void => {
    const id = campaign.campaign_id
    if (busy.has(id)) return
    setTarget(null)
    setBusy((prev) => new Set(prev).add(id))
    announce(concludedNow ? CONCLUDING : REOPENING)
    void setConcluded(id, concludedNow).then((outcome) => {
      setBusy((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
      if (!stillHere()) return
      if (outcome === 'done') {
        setTarget(concludedNow ? { kind: 'toggle' } : { kind: 'prep', id })
        announce(concludedNow ? CONCLUDED_NOTE : REOPENED)
      } else {
        announce(outcome === 'unavailable' ? UNAVAILABLE : CHANGE_FAILED)
      }
    })
  }

  const showMore = (): void => {
    const first = active[visibleActive]
    if (first !== undefined) setTarget({ kind: 'prep', id: first.campaign_id })
    setShown(visibleActive + TAVERN_PAGE_SIZE)
  }

  const orderedSeats = React.useMemo(() => orderSeats(seats.items), [seats.items])
  // A player with no seat at the end of a good read (E-1); a campaign account's
  // seats never replace its own campaigns' states.
  const noSeats = !enabled && seats.complete && orderedSeats.length === 0
  const where = (enabled ? complete && items.length === 0 : noSeats) ? latestConversation(store) : null
  const whereMode = where === null ? undefined : MODES.find((m) => m.mode === where.mode)

  const card = (campaign: Campaign, variant: 'active' | 'dormant' | 'concluded'): React.JSX.Element => (
    <li key={campaign.campaign_id}>
      <TavernCampaignCard
        campaign={campaign}
        variant={variant}
        busy={busy.has(campaign.campaign_id)}
        prepRef={prepRef(campaign.campaign_id)}
        landing={heading}
        now={now}
        onPrep={() => prep(campaign)}
        onConclude={() => change(campaign, true)}
        onReopen={() => change(campaign, false)}
      />
    </li>
  )

  return (
    <>
      <p role="status" className="tavern-screen__status">{announcement}</p>

      {where !== null && whereMode !== undefined && (
        <section className="tavern-screen__where" aria-labelledby={`${ids}-where`}>
          <h2 id={`${ids}-where`} className="tavern-screen__section-title">{WHERE_YOU_WERE}</h2>
          <p className="tavern-screen__message">{`${whereMode.label} · “${where.title}”`}</p>
          <div className="tavern-screen__actions">
            <Button variant="tonal" icon={whereMode.icon} onClick={() => openThread(where.id, where.mode)}>
              {BACK_TO_THREAD}
            </Button>
          </div>
        </section>
      )}

      {active.length > 0 && (
        <ul className="tavern-screen__grid" aria-label="Your campaigns">
          {active.slice(0, visibleActive).map((campaign) => card(campaign, campaign.dormant ? 'dormant' : 'active'))}
        </ul>
      )}
      {active.length > visibleActive && (
        <div className="tavern-screen__actions">
          <Button variant="text" onClick={showMore}>{SHOW_MORE}</Button>
        </div>
      )}

      {list.kind === 'loading' && items.length === 0 && (
        <>
          <ul className="tavern-screen__grid" aria-hidden="true">
            <li className="tavern-screen__skeleton" />
            <li className="tavern-screen__skeleton" />
          </ul>
          <p className="tavern-screen__message">{LOADING}</p>
        </>
      )}
      {complete && items.length === 0 && <p className="tavern-screen__message">{EMPTY}</p>}

      {!enabled && seats.reading && orderedSeats.length === 0 && (
        <>
          <ul className="tavern-screen__grid" aria-hidden="true">
            <li className="tavern-screen__skeleton" />
            <li className="tavern-screen__skeleton" />
          </ul>
          <p className="tavern-screen__message">{SEATS_LOADING}</p>
        </>
      )}
      {noSeats && <TavernNoSeats titleId={`${ids}-no-seats`} onAskTheSage={askTheSage} />}
      {orderedSeats.length > 0 && (
        <section className="tavern-screen__seats">
          <h2 className="tavern-screen__section-title">{SEATS_HEADING}</h2>
          <ul className="tavern-screen__grid" aria-label={SEATS_HEADING}>
            {orderedSeats.slice(0, shownSeats).map((seat) => (
              <li key={seat.campaign_id}>
                <TavernSeatCard seat={seat} now={now} />
              </li>
            ))}
          </ul>
          {orderedSeats.length > shownSeats && (
            <div className="tavern-screen__actions">
              <Button variant="text" onClick={() => setShownSeats(shownSeats + TAVERN_PAGE_SIZE)}>{SHOW_MORE_SEATS}</Button>
            </div>
          )}
        </section>
      )}

      {retryShown && (
        <div className="tavern-screen__row">
          {campaignsLine && <p className="tavern-screen__message">{LOAD_FAILED}</p>}
          {seatsLine && <p className="tavern-screen__message">{SEATS_FAILED}</p>}
          <PendingButton busy={campaignsReading || seats.reading} landing={heading} onPress={() => {
            pressRetry()
            setTrack('list')
            setRetrying({ campaigns: campaignsFailed, seats: seats.failed })
            if (campaignsFailed) {
              if (list.kind === 'failed') loadCampaigns()
              else loadMoreCampaigns()
            }
            if (seats.failed) seats.retry()
          }}>Retry</PendingButton>
        </div>
      )}
      {atCeiling && (
        <div className="tavern-screen__row">
          <PendingButton busy={list.loadingMore} landing={heading} onPress={() => {
            setTrack('list')
            loadMoreCampaigns()
          }}>Load more</PendingButton>
        </div>
      )}

      {enabled && <BeginAnewCard onCreated={onSelected} onAnnounce={announce} nameFieldRef={nameField} />}

      {concluded.length > 0 && (
        <section className="tavern-screen__concluded">
          <TavernActionButton
            variant="text"
            icon="inventory_2"
            ariaExpanded={open}
            ariaControls={regionId}
            onPress={() => setOpen(!open)}
            buttonRef={toggle}
          >
            {`Concluded (${concluded.length})`}
          </TavernActionButton>
          <ul id={regionId} className="tavern-screen__grid" aria-label="Concluded campaigns" hidden={!open}>
            {concluded.slice(0, shownConcluded).map((campaign) => card(campaign, 'concluded'))}
          </ul>
          {open && concluded.length > shownConcluded && (
            <div className="tavern-screen__actions">
              <Button variant="text" onClick={() => setShownConcluded(shownConcluded + TAVERN_PAGE_SIZE)}>
                {SHOW_MORE_CONCLUDED}
              </Button>
            </div>
          )}
        </section>
      )}
    </>
  )
}
