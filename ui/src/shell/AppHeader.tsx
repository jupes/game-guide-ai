/**
 * AppHeader — persistent channel switcher band (swe1.4).
 *
 * Sits under the TopBar so switching channels no longer depends on the LeftNav.
 * Renders the role-gated channels as accented filter chips wired to setMode,
 * with the shared theme control anchored at the right edge.
 *
 * On the narrow layout (agent-forge-harness-0rn) WorkspaceShell passes
 * `showSettings={false}`: ModelPicker and the theme control move into the
 * LeftNav drawer, and the band keeps only the channels, so a phone's header
 * is one row.
 *
 * The workspace reveal indicator (agent-forge-harness-1kg.7.3 PR-2, REVEAL-14) shares this
 * row, so it is on screen in every channel. It must stay operable above any modal, so the
 * shell cannot make the whole band `inert`: it passes `inert` here instead, and the band
 * makes inert everything but the indicator.
 */

import * as React from 'react'
import { Chip } from '../ds/Chip'
import { Switch } from '../ds/Switch'
import { useTheme } from '../ds/theme'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { ModelPicker } from './ModelPicker'
import { modesForRole, accentClass } from './modes'
import { RevealIndicator } from './RevealIndicator'
import './AppHeader.css'
import './modeAccents.css'

export interface AppHeaderProps {
  /** false on the narrow layout: ModelPicker and the theme control move to
   * the drawer. @default true */
  showSettings?: boolean
  /** A modal is open: everything in the band but the reveal indicator is inert. @default false */
  inert?: boolean
}

export function AppHeader({ showSettings = true, inert = false }: AppHeaderProps): React.JSX.Element {
  const { mode, setMode } = useAppNav()
  const { user } = useCurrentUser()
  const { theme, toggleTheme } = useTheme()
  const channelsRef = React.useRef<HTMLDivElement>(null)
  // If the last Stop removes the indicator while it holds focus, the selected channel takes it.
  const focusSelectedChannel = React.useCallback((): void => {
    channelsRef.current?.querySelector<HTMLElement>('[aria-pressed="true"]')?.focus()
  }, [])

  return (
    <nav className="app-header" aria-label="Channels">
      <div className="app-header__controls" inert={inert}>
        <div ref={channelsRef} className="app-header__channels">
          {modesForRole(user.role).map(({ mode: m, icon, label }) => (
            <Chip
              key={m}
              type="filter"
              icon={icon}
              label={label}
              selected={mode === m}
              onClick={() => setMode(m)}
              className={`app-header__channel ${accentClass(m)}`}
            />
          ))}
        </div>
      </div>

      <RevealIndicator fallbackFocus={focusSelectedChannel} />

      {showSettings && (
        <div className="app-header__controls" inert={inert}>
          <ModelPicker />

          <div className="app-header__theme">
            <span className="app-header__theme-label">Dark theme</span>
            <Switch
              checked={theme === 'dark'}
              onChange={toggleTheme}
              ariaLabel="Dark theme"
            />
          </div>
        </div>
      )}
    </nav>
  )
}
