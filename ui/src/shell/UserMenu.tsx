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
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        type="button"
        aria-label="Open user menu"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={toggleMenu}
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          padding: 0,
          borderRadius: '50%',
          minHeight: 44,
          minWidth: 44,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Avatar name={user.displayName} tone="gold" />
      </button>

      {open && (
        <div
          role="menu"
          style={{
            position: 'absolute',
            bottom: '100%',
            left: 0,
            background: 'var(--aether-surface)',
            border: '1px solid var(--aether-outline)',
            borderRadius: 'var(--aether-radius-card, 16px)',
            padding: '8px 0',
            minWidth: 160,
            boxShadow: 'var(--aether-elevation-2, 0 2px 8px rgba(0,0,0,0.15))',
            zIndex: 100,
          }}
        >
          <button
            type="button"
            role="menuitem"
            onClick={handleEditProfile}
            style={{
              display: 'block',
              width: '100%',
              padding: '10px 16px',
              background: 'none',
              border: 'none',
              textAlign: 'left',
              cursor: 'pointer',
              color: 'var(--aether-on-surface)',
              font: 'inherit',
              minHeight: 44,
            }}
          >
            Profile
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={handleSignOut}
            style={{
              display: 'block',
              width: '100%',
              padding: '10px 16px',
              background: 'none',
              border: 'none',
              textAlign: 'left',
              cursor: 'pointer',
              color: 'var(--aether-on-surface)',
              font: 'inherit',
              minHeight: 44,
            }}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}
