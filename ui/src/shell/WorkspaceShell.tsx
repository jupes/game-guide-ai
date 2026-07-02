/**
 * WorkspaceShell — Two-column workspace layout.
 *
 * Fixed 268px LeftNav + flexible main area with the mode-aware ChatPane.
 */

import * as React from 'react'
import { LeftNav } from './LeftNav'
import { TopBar } from './TopBar'
import { ChatPane } from './ChatPane'
import './WorkspaceShell.css'

export function WorkspaceShell(): React.JSX.Element {
  return (
    <div className="workspace-shell">
      <TopBar />

      <div className="workspace-shell__body">
        <LeftNav />

        <main className="workspace-shell__main">
          <ChatPane />
        </main>
      </div>
    </div>
  )
}
