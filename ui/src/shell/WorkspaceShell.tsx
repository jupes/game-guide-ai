/**
 * WorkspaceShell — Two-column workspace layout.
 *
 * Fixed 268px LeftNav + flexible main area with the mode-aware ChatPane.
 */

import * as React from 'react'
import { LeftNav } from './LeftNav'
import { TopBar } from './TopBar'
import { ChatPane } from './ChatPane'

export function WorkspaceShell(): React.JSX.Element {
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
            overflow: 'hidden',
            padding: 24,
          }}
        >
          <ChatPane />
        </main>
      </div>
    </div>
  )
}
