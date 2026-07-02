/**
 * UserMenu — Avatar trigger + popover menu for the current user.
 *
 * Shows the user's Avatar as a clickable button. When opened, displays
 * Profile and Sign out menu items.
 */

import * as React from 'react'
import { useState } from 'react'
import { Avatar } from '../ds/Avatar'
import { useCurrentUser } from './currentUser'
import './UserMenu.css'

export function UserMenu(): React.JSX.Element {
  const { user } = useCurrentUser()
  const [open, setOpen] = useState(false)

  function toggleMenu(): void {
    setOpen((prev) => !prev)
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
