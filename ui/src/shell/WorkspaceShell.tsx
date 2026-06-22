/**
 * WorkspaceShell — Two-column workspace layout.
 *
 * Fixed 268px LeftNav + flexible main area with a mode-aware chat pane placeholder.
 */

import * as React from 'react'
import { Card } from '../ds/Card'
import { useAppNav } from './AppNav'
import type { ChatMode } from './AppNav'
import { LeftNav } from './LeftNav'
import { TopBar } from './TopBar'

const MODE_LABELS: Record<ChatMode, string> = {
  sage: 'Sage',
  spell: 'Spell',
  rules: 'Rules',
  gm: 'GM',
}

export function WorkspaceShell(): React.JSX.Element {
  const { mode } = useAppNav()
  const modeLabel = MODE_LABELS[mode]

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        overflow: 'hidden',
        background: 'var(--aether-surface)',
      }}
    >
      <TopBar />

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <LeftNav />

        <main
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden auto',
            padding: 24,
          }}
        >
          {/* ChatPane placeholder */}
          <Card
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <p
              style={{
                margin: 0,
                fontSize: 18,
                color: 'var(--aether-on-surface-variant)',
              }}
            >
              Ask the {modeLabel}…
            </p>
          </Card>
        </main>
      </div>
    </div>
  )
}
