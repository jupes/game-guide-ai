/**
 * TopBar — Brand and active-conversation header.
 *
 * On the narrow layout (agent-forge-harness-0rn, LAYOUT-3) WorkspaceShell
 * passes `navToggle`, and the TopBar leads with the button that opens
 * LeftNav as a drawer. It is a raw <button>, not an IconButton: IconButton
 * always renders `aria-pressed`, which would announce a disclosure as a
 * toggle. It sits INSIDE the brand group so the bar's two-child
 * `space-between` row does not spread into three.
 */

import * as React from 'react'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'
import './TopBar.css'

const OPEN_NAVIGATION = 'Open navigation'

export interface TopBarNavToggle {
  /** Drives aria-expanded. */
  expanded: boolean
  /** The id of the drawer host (aria-controls). */
  controls: string
  /** Opens the drawer. Idempotent; never toggles. */
  onOpen: () => void
  buttonRef: React.RefObject<HTMLButtonElement | null>
}

export interface TopBarProps {
  /** Rendered only when present: the narrow layout's menu button. */
  navToggle?: TopBarNavToggle
}

/** The menu button, from the toggle's parts: each is its own binding, so the
 * ref is only ever handed to `ref`, never read while rendering. */
function NavMenuButton({ expanded, controls, onOpen, buttonRef }: TopBarNavToggle): React.JSX.Element {
  return (
    <button
      ref={buttonRef}
      type="button"
      className="top-bar__menu"
      aria-label={OPEN_NAVIGATION}
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={onOpen}
    >
      <span className="material-symbols-rounded" aria-hidden="true">
        menu
      </span>
    </button>
  )
}

export function TopBar({ navToggle }: TopBarProps): React.JSX.Element {
  const { conversationId } = useAppNav()
  const store = useConversationStore()
  const activeConversation =
    conversationId === null ? undefined : store.get(conversationId)

  return (
    <header className="top-bar">
      <div className="top-bar__brand">
        {navToggle && <NavMenuButton {...navToggle} />}
        <span
          className="material-symbols-rounded top-bar__brand-icon"
          aria-hidden="true"
        >
          auto_stories
        </span>
        <span className="top-bar__brand-name">Aetheril</span>
      </div>
      {activeConversation && (
        <span className="top-bar__conversation-title" aria-live="polite">
          {activeConversation.title}
        </span>
      )}
    </header>
  )
}
