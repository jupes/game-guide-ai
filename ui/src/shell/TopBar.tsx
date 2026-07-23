/**
 * TopBar — Brand and active-conversation header.
 */

import * as React from 'react'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'
import './TopBar.css'

export function TopBar(): React.JSX.Element {
  const { conversationId } = useAppNav()
  const store = useConversationStore()
  const activeConversation =
    conversationId === null ? undefined : store.get(conversationId)

  return (
    <header className="top-bar">
      <div className="top-bar__brand">
        <span
          className="material-symbols-rounded top-bar__brand-icon"
          aria-hidden="true"
        >
          auto_stories
        </span>
        <span className="top-bar__brand-name">Aetheril</span>
      </div>
      {activeConversation && (
        <span className="top-bar__conversation-title" aria-live="polite">
          {activeConversation.title}
        </span>
      )}
    </header>
  )
}
