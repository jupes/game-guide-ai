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
import type { Conversation } from './conversationStore'
import { MODES } from './modes'

export function LeftNav(): React.JSX.Element {
  const { mode, setMode, conversationId, setConversationId } = useAppNav()
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
    <nav
      style={{
        width: 268,
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        background: 'var(--aether-surface-container)',
        borderRight: '1px solid var(--aether-outline-variant)',
      }}
      aria-label="Main navigation"
    >
      {/* Brand logo area */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '16px 16px 8px',
          flexShrink: 0,
        }}
      >
        <span
          className="material-symbols-rounded"
          aria-hidden="true"
          style={{ fontSize: 28, color: 'var(--aether-primary)' }}
        >
          auto_stories
        </span>
        <span style={{ fontWeight: 700, fontSize: 20, color: 'var(--aether-on-surface)' }}>
          Aetheril
        </span>
      </div>

      {/* Mode chips */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          padding: '8px 8px',
          flexShrink: 0,
        }}
      >
        {MODES.map(({ mode: m, icon, label }) => (
          <Chip
            key={m}
            type="filter"
            icon={icon}
            label={label}
            selected={mode === m}
            onClick={() => setMode(m)}
            style={{ minHeight: 44 }}
          />
        ))}
      </div>

      {/* Conversation list */}
      <div
        style={{
          flex: 1,
          padding: '16px 16px 8px',
          overflow: 'hidden auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 4,
          }}
        >
          <p
            style={{
              margin: 0,
              fontSize: 12,
              fontWeight: 600,
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              color: 'var(--aether-on-surface-variant)',
            }}
          >
            Conversations
          </p>
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
            aria-pressed={conversationId === conv.id ? true : false}
            style={{
              display: 'block',
              width: '100%',
              minHeight: 44,
              textAlign: 'left',
              padding: '8px 12px',
              border: 'none',
              borderRadius: 8,
              background:
                conversationId === conv.id
                  ? 'var(--aether-secondary-container)'
                  : 'transparent',
              color:
                conversationId === conv.id
                  ? 'var(--aether-on-secondary-container)'
                  : 'var(--aether-on-surface)',
              cursor: 'pointer',
              fontSize: 14,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {conv.title}
          </button>
        ))}
      </div>

      {/* Bottom row: UserMenu */}
      <div
        style={{
          padding: '8px 16px 16px',
          borderTop: '1px solid var(--aether-outline-variant)',
          flexShrink: 0,
        }}
      >
        <UserMenu />
      </div>
    </nav>
  )
}
