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

  // Conversation list is derived from the store on each render — no local state needed.
  // A forceUpdate counter is used only to trigger re-render after mutations.
  const [_tick, setTick] = React.useState(0)
  void _tick // read to satisfy exhaustive-deps; value is intentionally unused
  const convs: Conversation[] = store.list(mode)

  const handleNew = React.useCallback(() => {
    const conv = store.create(mode)
    setTick((t) => t + 1) // trigger re-render to pick up new conversation
    setConversationId(conv.id)
  }, [mode, store, setConversationId])

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

        {convs.map((conv) => (
          <button
            key={conv.id}
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
        ))}
      </div>

      {/* Bottom row: UserMenu */}
      <div className="left-nav__footer">
        <UserMenu />
      </div>
    </nav>
  )
}
