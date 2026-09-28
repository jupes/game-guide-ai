/**
 * WorkspaceShell — Two-column workspace layout.
 *
 * Fixed 268px LeftNav + flexible main area with the mode-aware ChatPane.
 */

import * as React from 'react'
import { LeftNav } from './LeftNav'
import { TopBar } from './TopBar'
import { AppHeader } from './AppHeader'
import { ChatPane } from './ChatPane'
import { ModelCatalogProvider } from './ModelCatalogContext'
import './WorkspaceShell.css'

export function WorkspaceShell(): React.JSX.Element {
  // agent-forge-harness-bta: AppHeader's ModelPicker loads the /models
  // catalog and ChatPane sends by it, so both read the one copy held here.
  return (
    <ModelCatalogProvider>
      <div className="workspace-shell">
        <TopBar />
        <AppHeader />

        <div className="workspace-shell__body">
          <LeftNav />

          <main className="workspace-shell__main">
            <ChatPane />
          </main>
        </div>
      </div>
    </ModelCatalogProvider>
  )
}
