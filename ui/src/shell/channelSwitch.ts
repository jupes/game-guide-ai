import { useContext } from 'react'
import type { ChatMode } from '../api'
import { useAppNav } from './AppNav'
import { useCampaign } from './campaignContext'
import { CampaignThreadsContext } from './campaignThreads'
import { newestFirst, serverOnlyRows, useConversationIndex } from './conversationIndex'
import { useConversationStore } from './ConversationStoreContext'

/** The drawer and Landing select the newest conversation in the destination.
 * An empty destination is the unsent composer, with no placeholder row. */
export function useChannelSwitch(): (mode: ChatMode, enterWorkspace?: boolean) => void {
  const nav = useAppNav()
  const { selection } = useCampaign()
  const threads = useContext(CampaignThreadsContext)
  const store = useConversationStore()
  const { rows } = useConversationIndex()

  return (mode, enterWorkspace = false) => {
    if (!enterWorkspace && nav.mode === mode) return
    let id: string | null
    if (mode === 'gm' && selection.kind !== 'none') {
      // Read an already loaded list only. Entering from Landing must not start
      // a campaign request for a channel the user has not entered yet.
      id = threads?.scope ? threads.store.getSnapshot().get(threads.scope.key)?.rows[0]?.id ?? null : null
    } else {
      id = [...store.list(mode).reverse(), ...serverOnlyRows(rows, store, mode)].sort(newestFirst)[0]?.id ?? null
    }
    if (enterWorkspace) nav.enterWorkspace(mode)
    else nav.setMode(mode)
    nav.setConversationId(id)
  }
}
