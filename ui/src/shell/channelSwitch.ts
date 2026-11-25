import { useContext } from 'react'
import type { ChatMode } from '../api'
import { useAppNav } from './AppNav'
import { useCampaign } from './campaignContext'
import { CampaignThreadsContext } from './campaignThreads'
import { newestFirst, serverOnlyRows, useConversationIndex } from './conversationIndex'
import { useConversationStore } from './ConversationStoreContext'
import { useCanvasActions } from './canvasContext'

/** The drawer and Landing select the newest conversation in the destination.
 * An empty destination is the unsent composer, with no placeholder row. */
export function useChannelSwitch(): (mode: ChatMode, enterWorkspace?: boolean, onSwitched?: () => void) => void {
  const nav = useAppNav()
  const { selection, parkedConversation } = useCampaign()
  const { switchWithCanvas } = useCanvasActions()
  const threads = useContext(CampaignThreadsContext)
  const store = useConversationStore()
  const { rows } = useConversationIndex()

  return (mode, enterWorkspace = false, onSwitched) => {
    if (!enterWorkspace && nav.mode === mode) {
      onSwitched?.()
      return
    }
    const switchNow = (): void => {
      let id: string | null
      if (mode === 'gm' && selection.kind !== 'none') {
        const parked = parkedConversation()
        const newest = threads?.scope ? threads.store.getSnapshot().get(threads.scope.key)?.rows[0]?.id ?? null : null
        id = parked === undefined ? newest : parked
      } else {
        id = [...store.list(mode).reverse(), ...serverOnlyRows(rows, store, mode)].sort(newestFirst)[0]?.id ?? null
      }
      if (enterWorkspace) nav.enterWorkspace(mode)
      else nav.setMode(mode)
      nav.setConversationId(id)
      onSwitched?.()
    }
    if (nav.mode === 'gm' && mode !== 'gm') switchWithCanvas(switchNow)
    else switchNow()
  }
}
