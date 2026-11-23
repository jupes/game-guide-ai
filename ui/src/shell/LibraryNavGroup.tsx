/**
 * LibraryNavGroup -- the "Campaign Library" group of LeftNav
 * (agent-forge-harness-1kg.6.4; brief 2.5; LIB-7, LIB-8, INFERRED I-5).
 *
 * One row per available category opens the Library panel at that category.
 * The lists load in the panel when the GM asks.
 *
 * - A row is a disclosure of the panel: `aria-controls` names it and `aria-expanded`
 *   says whether it is open at that category.
 * - Pressing one opens the panel BEFORE the drawer closes, so the panel records where
 *   focus returns: the row itself, or, for a row inside the open nav drawer, the control
 *   that opened the drawer (the canvas's C-3a rule, shared through `drawerOpenerRef`).
 * - It is not a region: the panel owns the name "Campaign Library" as a region.
 *
 * LeftNav renders it only while the Workbench is active, so a player, another channel
 * and a GM with no campaign never see it (X-9).
 */

import * as React from 'react'
import { useCanvasActions } from './canvasContext'
import { LIBRARY_PANEL_ID, useLibraryPanel, type LibraryCategoryId } from './libraryPanel'
import { LIBRARY_COPY } from './workbenchCopy'
import './LibraryNavGroup.css'

/** The registry's own icons for the types each category holds (person, shield, description, history_edu). */
const ICON: Readonly<Record<LibraryCategoryId, string>> = {
  npcs: 'person',
  bestiary: 'shield',
  documents: 'description',
  'session-log': 'history_edu',
  cues: 'music_note',
}

export interface LibraryNavGroupProps {
  /** Called AFTER the panel opens: the narrow and medium drawers close on it. */
  onNavigate?: () => void
  /** Called after the panel opens and before `onNavigate`, so the shell can hand focus to the panel as the drawer closes. */
  onOpenLibrary?: () => void
}

export function LibraryNavGroup({ onNavigate, onOpenLibrary }: LibraryNavGroupProps): React.JSX.Element {
  const { open, category, tabs, openLibrary } = useLibraryPanel()
  const { drawerOpenerRef } = useCanvasActions()
  const titleId = React.useId()

  const press = (id: LibraryCategoryId, row: HTMLElement): void => {
    const drawerOpener = drawerOpenerRef.current
    const opener = row.closest('[data-workbench-drawer]') !== null && drawerOpener?.isConnected === true ? drawerOpener : row
    openLibrary(id, opener)
    onOpenLibrary?.()
    onNavigate?.()
  }

  return (
    <div className="library-nav">
      <p id={titleId} className="library-nav__title">
        {LIBRARY_COPY.title}
      </p>
      <ul className="library-nav__list" aria-labelledby={titleId}>
        {tabs.map((id) => (
          <li key={id}>
            <button
              type="button"
              className="library-nav__row"
              aria-controls={LIBRARY_PANEL_ID}
              aria-expanded={open && category === id}
              onClick={(event) => press(id, event.currentTarget)}
            >
              <span className="material-symbols-rounded library-nav__icon" aria-hidden="true">
                {ICON[id]}
              </span>
              <span>{id === 'cues' ? 'Cues' : LIBRARY_COPY.category[id].tab}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
