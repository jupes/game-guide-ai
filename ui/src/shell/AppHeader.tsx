/**
 * AppHeader — persistent channel switcher band (swe1.4).
 *
 * Sits under the TopBar so switching channels no longer depends on the LeftNav.
 * Renders the role-gated channels as accented filter chips wired to setMode, and
 * reserves a documented slot for future note-taking / GM-lore nav (swe1.5) so
 * those can be added without a layout redesign.
 */

import * as React from 'react'
import { Chip } from '../ds/Chip'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { modesForRole, accentClass } from './modes'
import './AppHeader.css'
import './modeAccents.css'

export function AppHeader(): React.JSX.Element {
  const { mode, setMode } = useAppNav()
  const { user } = useCurrentUser()

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

      {/* Reserved for future note-taking + GM-lore nav (swe1.5). Kept empty and
          hidden from assistive tech until those land, so adding them later does
          not reflow the header. */}
      <div className="app-header__future-slot" aria-hidden="true" />
    </nav>
  )
}
