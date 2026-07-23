/**
 * AppHeader — persistent channel switcher band (swe1.4).
 *
 * Sits under the TopBar so switching channels no longer depends on the LeftNav.
 * Renders the role-gated channels as accented filter chips wired to setMode,
 * with the shared theme control anchored at the right edge.
 */

import * as React from 'react'
import { Chip } from '../ds/Chip'
import { Switch } from '../ds/Switch'
import { useTheme } from '../ds/theme'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { modesForRole, accentClass } from './modes'
import './AppHeader.css'
import './modeAccents.css'

export function AppHeader(): React.JSX.Element {
  const { mode, setMode } = useAppNav()
  const { user } = useCurrentUser()
  const { theme, toggleTheme } = useTheme()

  return (
    <nav className="app-header" aria-label="Channels">
      <div className="app-header__channels">
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

      <div className="app-header__theme">
        <span className="app-header__theme-label">Dark theme</span>
        <Switch
          checked={theme === 'dark'}
          onChange={toggleTheme}
          ariaLabel="Dark theme"
        />
      </div>
    </nav>
  )
}
