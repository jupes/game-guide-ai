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
import { useRoleToggle } from './useRoleToggle'
import './UserMenu.css'

export function UserMenu(): React.JSX.Element {
  const { user } = useCurrentUser()
  const { openProfile } = useAppNav()
  const toggleRole = useRoleToggle()
  const [open, setOpen] = useState(false)

  function toggleMenu(): void {
    setOpen((prev) => !prev)
  }

  function handleSignOut(): void {
    setOpen(false)
    user.signOut()
  }

  function handleOpenProfile(): void {
    setOpen(false)
    openProfile()
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
        <Avatar name={user.displayName} tone={user.avatarTone ?? 'gold'} />
      </button>

      {open && (
        <div role="menu" className="user-menu__popover">
          <div className="user-menu__item user-menu__role">
            <span id="user-menu-role-label">Dungeon Master</span>
            <Switch
              checked={user.role === 'dm'}
              onChange={toggleRole}
              ariaLabel="Dungeon Master role"
            />
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={handleOpenProfile}
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
