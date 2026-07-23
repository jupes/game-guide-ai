/**
 * LeftNav — Fixed sidebar navigation.
 *
 * Brand logo, mode selection chips, conversation list, and UserMenu.
 */

import * as React from 'react'
import { Chip } from '../ds/Chip'
import { IconButton } from '../ds/IconButton'
import { useAppNav } from './AppNav'
import { UserMenu } from './UserMenu'
import { useConversationStore } from './ConversationStoreContext'
import { useCurrentUser } from './currentUser'
import type { Conversation } from './conversationStore'
import { modesForRole, accentClass } from './modes'
import './LeftNav.css'
import './modeAccents.css'

export function LeftNav(): React.JSX.Element {
  const { mode, setMode, conversationId, setConversationId } = useAppNav()
  const { user } = useCurrentUser()
  const store = useConversationStore()
  const [renameState, setRenameState] = React.useState<{
    id: string
    title: string
  } | null>(null)
  const convs: Conversation[] = store.list(mode)

  const handleNew = React.useCallback(() => {
    const conv = store.create(mode)
    setConversationId(conv.id)
  }, [mode, store, setConversationId])

  const saveRename = React.useCallback((id: string) => {
    if (renameState?.id !== id) return
    store.rename(id, renameState.title)
    setRenameState(null)
  }, [renameState, store])

  return (
    <nav className="left-nav" aria-label="Main navigation">
      {/* Brand lives once in the TopBar header (swe1.10) — the sidebar starts
          straight at the mode chips to avoid a duplicate 'Aetheril'. */}

      {/* Mode chips */}
      <div className="left-nav__modes">
        {modesForRole(user.role).map(({ mode: m, icon, label }) => (
          <Chip
            key={m}
            type="filter"
            icon={icon}
            label={label}
            selected={mode === m}
            onClick={() => setMode(m)}
            className={`left-nav__mode-chip ${accentClass(m)}`}
          />
        ))}
      </div>

      {/* Conversation list */}
      <div className="left-nav__conversations">
        <div className="left-nav__conversations-header">
          <p className="left-nav__conversations-title">Conversations</p>
          <IconButton
            icon="add"
            ariaLabel="New conversation"
            size="small"
            onClick={handleNew}
          />
        </div>

        {convs.map((conv) => {
          const isRenaming = renameState?.id === conv.id
          return (
            <div className="left-nav__conversation-row" key={conv.id}>
              {isRenaming ? (
                <input
                  className="left-nav__conversation-input"
                  aria-label={`Conversation title for ${conv.title}`}
                  value={renameState.title}
                  autoFocus
                  onChange={(event) => {
                    setRenameState({ id: conv.id, title: event.target.value })
                  }}
                  onBlur={() => saveRename(conv.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      saveRename(conv.id)
                    } else if (event.key === 'Escape') {
                      event.preventDefault()
                      setRenameState(null)
                    }
                  }}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => setConversationId(conv.id)}
                  aria-pressed={conversationId === conv.id}
                  className={
                    conversationId === conv.id
                      ? 'left-nav__conversation left-nav__conversation--selected'
                      : 'left-nav__conversation'
                  }
                >
                  {conv.title}
                </button>
              )}
              <IconButton
                icon="edit"
                ariaLabel={`Rename ${conv.title}`}
                size="small"
                className="left-nav__conversation-rename"
                onClick={() => {
                  setRenameState({ id: conv.id, title: conv.title })
                }}
              />
            </div>
          )
        })}
      </div>

      {/* Bottom row: UserMenu */}
      <div className="left-nav__footer">
        <UserMenu />
      </div>
    </nav>
  )
}
