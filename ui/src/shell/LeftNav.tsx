/**
 * LeftNav — Fixed sidebar navigation.
 *
 * Brand logo, mode selection chips, conversation list placeholder, and UserMenu.
 */

import * as React from 'react'
import { Chip } from '../ds/Chip'
import { useAppNav } from './AppNav'
import type { ChatMode } from './AppNav'
import { UserMenu } from './UserMenu'

interface ModeEntry {
  mode: ChatMode
  icon: string
  label: string
}

const MODES: ModeEntry[] = [
  { mode: 'sage', icon: 'auto_stories', label: 'Sage' },
  { mode: 'spell', icon: 'auto_awesome', label: 'Spell' },
  { mode: 'rules', icon: 'gavel', label: 'Rules' },
  { mode: 'gm', icon: 'castle', label: 'GM' },
]

export function LeftNav(): React.JSX.Element {
  const { mode, setMode } = useAppNav()

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

      {/* Conversation list placeholder */}
      <div
        style={{
          flex: 1,
          padding: '16px 16px 8px',
          overflow: 'hidden auto',
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
