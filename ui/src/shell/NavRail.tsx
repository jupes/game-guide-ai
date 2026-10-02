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
 * - **Campaign documents**, only while the Workbench is active, opens the drawer
 *   and lands on the documents list (LIB-8's single library icon; 1kg.6.4
 *   repoints it at the Library panel).
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
import { WORKBENCH_COPY } from './workbenchCopy'
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
  /** Present only while the Workbench is active (X-9): opens the drawer at the documents list. */
  onOpenDocuments?: () => void
  /** The shell makes the rail inert behind the drawer and the loss-guard dialog. */
  inert?: boolean
}

export function NavRail({
  controls,
  expanded,
  onOpen,
  openButtonRef,
  onOpenDocuments,
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
      {onOpenDocuments !== undefined && (
        <button type="button" className="nav-rail__button" aria-label={WORKBENCH_COPY.campaignDocuments} onClick={onOpenDocuments}>
          <span className="material-symbols-rounded" aria-hidden="true">
            folder_open
          </span>
        </button>
      )}
    </nav>
  )
}
