/**
 * UserMenu — Avatar trigger + popover menu for the current user.
 *
 * Shows the user's Avatar as a clickable button. When opened, displays
 * Profile and Sign out menu items.
 */

import * as React from 'react'
import { useState } from 'react'
import { Avatar } from '../ds/Avatar'
import { Switch } from '../ds/Switch'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { useTheme } from '../ds/theme'
import './UserMenu.css'

export function UserMenu(): React.JSX.Element {
  const { user, setRole } = useCurrentUser()
  const { mode, setMode } = useAppNav()
  const { theme, setTheme } = useTheme()
  const [open, setOpen] = useState(false)

  function toggleMenu(): void {
    setOpen((prev) => !prev)
  }

  function handleRoleToggle(next: boolean): void {
    const role = next ? 'dm' : 'player'
    setRole(role)
    // The GM channel is DM-only: giving up the DM role while sitting in it
    // falls back to the sage channel (channel-chats CP-D).
    if (role === 'player' && mode === 'gm') {
      setMode('sage')
    }
  }

  function handleSignOut(): void {
    setOpen(false)
    user.signOut()
  }

  function handleEditProfile(): void {
    setOpen(false)
    user.editProfile()
  }

  return (
    <div className="user-menu">
      <button
        type="button"
        aria-label="Open user menu"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={toggleMenu}
        className="user-menu__trigger"
      >
        <Avatar name={user.displayName} tone="gold" />
      </button>

      {open && (
        <div role="menu" className="user-menu__popover">
          <div className="user-menu__item user-menu__role">
            <span id="user-menu-role-label">Dungeon Master</span>
            <Switch
              checked={user.role === 'dm'}
              onChange={handleRoleToggle}
              ariaLabel="Dungeon Master role"
            />
          </div>
          <div className="user-menu__item user-menu__role">
            <span id="user-menu-theme-label">Dark theme</span>
            <Switch
              checked={theme === 'dark'}
              onChange={(next) => setTheme(next ? 'dark' : 'light')}
              ariaLabel="Dark theme"
            />
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={handleEditProfile}
            className="user-menu__item"
          >
            Profile
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={handleSignOut}
            className="user-menu__item"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}
