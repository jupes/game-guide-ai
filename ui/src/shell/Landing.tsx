/**
 * Landing — Aetheril-branded entry screen.
 *
 * Shows the brand, tagline, a primary CTA, and optional mode entry chips.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { Card } from '../ds/Card'
import { Chip } from '../ds/Chip'
import { useAppNav } from './AppNav'
import type { ChatMode } from './AppNav'

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

export function Landing(): React.JSX.Element {
  const { enterWorkspace } = useAppNav()

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--aether-surface)',
        padding: 24,
      }}
    >
      <Card
        style={{
          maxWidth: 480,
          width: '100%',
          textAlign: 'center',
        }}
      >
        {/* Brand */}
        <div style={{ marginBottom: 8 }}>
          <span
            className="material-symbols-rounded"
            aria-hidden="true"
            style={{ fontSize: 48, color: 'var(--aether-primary)' }}
          >
            auto_stories
          </span>
        </div>

        <h1
          style={{
            margin: '0 0 4px',
            fontSize: 32,
            fontWeight: 700,
            color: 'var(--aether-on-surface)',
          }}
        >
          Aetheril
        </h1>

        <p
          style={{
            margin: '0 0 32px',
            fontSize: 16,
            color: 'var(--aether-on-surface-variant)',
          }}
        >
          Grounded answers from the rulebooks
        </p>

        {/* Primary CTA */}
        <div style={{ marginBottom: 24 }}>
          <Button
            variant="filled"
            size="large"
            icon="login"
            onClick={() => enterWorkspace()}
            style={{ minHeight: 44 }}
          >
            Enter the Tavern
          </Button>
        </div>

        {/* Mode entry chips */}
        <div
          style={{
            display: 'flex',
            gap: 8,
            justifyContent: 'center',
            flexWrap: 'wrap',
          }}
        >
          {MODES.map(({ mode, icon, label }) => (
            <Chip
              key={mode}
              type="suggestion"
              icon={icon}
              label={label}
              onClick={() => enterWorkspace(mode)}
              style={{ minHeight: 44 }}
            />
          ))}
        </div>
      </Card>
    </div>
  )
}
