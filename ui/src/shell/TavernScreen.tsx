/**
 * TavernScreen -- the campaign screen at /tavern (agent-forge-harness-74j).
 *
 * The shell's way into campaigns: `1kg.2.5`'s CampaignPicker, whose loading,
 * empty and error states are interactions ADR section 12.2's, then Continue
 * without a campaign and Back to chat. `30c` replaces this body with the full
 * tavern. Only an account that can use campaigns reaches it
 * (`useCampaign().enabled`); any other is sent to Landing before paint. It
 * reads nothing from the URL, writes nothing to web storage or the page title,
 * and adds no live region: the picker's status node is the page's only one.
 */
import * as React from 'react'
import { Button } from '../ds/Button'
import { useAppNav } from './AppNav'
import { CampaignPicker } from './CampaignPicker'
import { useCampaign } from './campaignContext'
import { useCurrentUser } from './currentUser'
import './TavernScreen.css'

// Copy (74j I-12): constants the design lane (`cub`, `30c`) may replace.
const HEADING = 'Your Campaigns'
const CONTINUE_WITHOUT = 'Continue without a campaign'
const BACK_TO_CHAT = 'Back to chat'

export function TavernScreen(): React.JSX.Element | null {
  const { enabled, selection, loadCampaigns, clearCampaign } = useCampaign()
  const { navIntent, mode, setMode, setConversationId, enterWorkspace, backToWorkspace, backToLanding } = useAppNav()
  const { user } = useCurrentUser()
  const root = React.useRef<HTMLElement>(null)
  // What a continuation checks when it resolves: this screen is still
  // mounted, for the same account (74j critic 5).
  const live = React.useRef({ mounted: false, userId: user.id })
  const [arrivedInApp] = React.useState(navIntent === 'push')

  // 74j I-5 and critic 6: an account that cannot use campaigns leaves before
  // paint, with no history entry, and never in the GM channel.
  React.useLayoutEffect(() => {
    if (enabled) return
    if (mode === 'gm') setMode('sage')
    backToLanding('replace')
  }, [enabled, mode, setMode, backToLanding])

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

  // 74j I-9: one list read per visit. The picker reads only from `idle`, and
  // the provider ignores a call while a read is in flight.
  React.useEffect(() => {
    if (enabled) loadCampaigns()
  }, [enabled, loadCampaigns])

  // 74j I-10: an in-app arrival focuses the page heading (the picker's,
  // promoted to h1); a cold load moves nothing.
  React.useLayoutEffect(() => {
    if (arrivedInApp) root.current?.querySelector<HTMLElement>('h1')?.focus()
  }, [arrivedInApp])

  if (!enabled) return null

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

  return (
    <main ref={root} className="tavern-screen">
      <div className="tavern-screen__column">
        <CampaignPicker pageHeading={HEADING} onSelected={onSelected} />
        <div className="tavern-screen__actions">
          {selection.kind !== 'none' && (
            <Button variant="outlined" onClick={continueWithout}>{CONTINUE_WITHOUT}</Button>
          )}
          <Button variant="filled" icon="arrow_back" onClick={backToWorkspace}>{BACK_TO_CHAT}</Button>
        </div>
      </div>
    </main>
  )
}
