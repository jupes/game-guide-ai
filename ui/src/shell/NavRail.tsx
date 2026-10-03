/**
 * NavRail -- the 56 px navigation rail (agent-forge-harness-1kg.6.3; interactions
 * ADR LAYOUT-7, brief 2.8, Critic C-9).
 *
 * At the medium layout (768 to 1023 px) the workspace shows a rail beside one
 * column, for every channel and every role, and at the wide layout while a
 * document is open (the canvas needs the width the sidebar would take). The rail
 * holds only the controls the drawer needs to be reached from it:
 *
 * - **Open navigation** opens the same modal drawer the narrow layout's TopBar
 *   button does; LeftNav lives in it, and never remounts.
 * - **Campaign Library**, only while the Workbench is active, toggles the Library panel
 *   (LIB-8's single library icon; 1kg.6.4). It names the panel it controls and says
 *   whether it is open; its tooltip is a native `title` equal to its name (INFERRED I-9:
 *   the design system has no Tooltip yet).
 *
 * Channels stay in AppHeader, which already carries them at every width, so the
 * rail does not repeat them (I-3).
 *
 * It is `nav "Navigation rail"`, not "Workbench rail": it exists for every role at
 * medium, and "rail" in the ADR means the tool rail (RAIL-*) (C-9). The icon
 * ligatures are decorative; every control is named. The shell sets `inert` while
 * the drawer or the loss-guard dialog is open.
 */

import * as React from 'react'
import { LIBRARY_COPY, WORKBENCH_COPY } from './workbenchCopy'
import './NavRail.css'

export interface NavRailProps {
  /** The id of the drawer host (aria-controls). */
  controls: string
  /** Drives aria-expanded. */
  expanded: boolean
  /** Opens the drawer. Idempotent; never toggles. */
  onOpen: () => void
  /** The shell focuses this button when the drawer closes. */
  openButtonRef: React.RefObject<HTMLButtonElement | null>
  /** Present only while the Workbench is active (X-9): toggles the Library panel. Given the button, which is where focus returns. */
  onToggleLibrary?: (opener: HTMLElement) => void
  /** Drives the Campaign Library button's aria-expanded. */
  libraryExpanded?: boolean
  /** The id of the Library panel (aria-controls). */
  libraryControls?: string
  /** The shell falls back to this button when the control that opened the panel is gone. */
  libraryButtonRef?: React.RefObject<HTMLButtonElement | null>
  /** The shell makes the rail inert behind the drawer and the loss-guard dialog. */
  inert?: boolean
}

export function NavRail({
  controls,
  expanded,
  onOpen,
  openButtonRef,
  onToggleLibrary,
  libraryExpanded = false,
  libraryControls,
  libraryButtonRef,
  inert,
}: NavRailProps): React.JSX.Element {
  return (
    <nav className="nav-rail" aria-label={WORKBENCH_COPY.railLabel} inert={inert}>
      <button
        ref={openButtonRef}
        type="button"
        className="nav-rail__button"
        aria-label={WORKBENCH_COPY.openNavigation}
        aria-expanded={expanded}
        aria-controls={controls}
        onClick={onOpen}
      >
        <span className="material-symbols-rounded" aria-hidden="true">
          menu
        </span>
      </button>
      {onToggleLibrary !== undefined && (
        <button
          ref={libraryButtonRef}
          type="button"
          className="nav-rail__button"
          aria-label={LIBRARY_COPY.title}
          title={LIBRARY_COPY.title}
          aria-expanded={libraryExpanded}
          aria-controls={libraryControls}
          onClick={(event) => onToggleLibrary(event.currentTarget)}
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            collections_bookmark
          </span>
        </button>
      )}
    </nav>
  )
}
