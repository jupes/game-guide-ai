/**
 * UserMenu — Avatar trigger + popover for the current user.
 *
 * Shows the user's Avatar as a clickable button. When opened, displays a
 * labelled group of two buttons (Profile, Sign out) — agent-forge-harness-3j4
 * dropped the ARIA menu/menuitem roles: Tab-only navigation with no arrow
 * keys, no Home/End and no roving tabindex made the promise of the APG menu
 * keyboard pattern without keeping it, so the popover is now what the
 * interaction actually is — a disclosure, not a menu. Escape closes it and
 * returns focus to the trigger; a press outside the popover closes it too
 * (without moving focus — the press already placed it where the user aimed).
 */

import * as React from 'react'
import { useState } from 'react'
import { Avatar } from '../ds/Avatar'
import { Switch } from '../ds/Switch'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import './UserMenu.css'

export function UserMenu(): React.JSX.Element {
  const { user } = useCurrentUser()
  const { openProfile } = useAppNav()
  const [open, setOpen] = useState(false)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const rootRef = React.useRef<HTMLDivElement>(null)
  const triggerRef = React.useRef<HTMLButtonElement>(null)

  function toggleMenu(): void {
    setOpen((prev) => !prev)
  }

  async function handleSignOut(): Promise<void> {
    setSignOutError(null)
    // Only the server can clear the httpOnly session cookie — if it refuses,
    // say so and stay signed in rather than showing a false "signed out".
    const ok = await user.signOut()
    if (ok) {
      setOpen(false)
    } else {
      setSignOutError("Couldn't sign out — please try again.")
    }
  }

  function handleOpenProfile(): void {
    setOpen(false)
    openProfile()
  }

  // agent-forge-harness-3j4 (R-9) — an outside pointer press closes the
  // popover; focus is NOT moved (the press already placed it where the user
  // aimed). Registered only while open and removed in the cleanup, so
  // nothing listens once the popover is closed; setState fires only from
  // the listener itself, never during render or an effect body.
  React.useEffect(() => {
    if (!open) return
    function handlePointerDown(e: PointerEvent): void {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', handlePointerDown)
    return () => document.removeEventListener('pointerdown', handlePointerDown)
  }, [open])

  // agent-forge-harness-3j4 (R-3) — Escape closes the popover and returns
  // focus to the trigger, but ONLY while focus is inside this root: React's
  // synthetic `onKeyDown` here only fires for a keydown whose target is a
  // descendant of `rootRef`, so Escape pressed on some other control on the
  // page (with the popover left open — it does not close on a mere Tab past
  // it, LAYOUT-10) is not this control's to intercept.
  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>): void {
    if (e.key === 'Escape' && open) {
      setOpen(false)
      triggerRef.current?.focus()
    }
  }

  return (
    <div className="user-menu" ref={rootRef} onKeyDown={handleKeyDown}>
      <button
        ref={triggerRef}
        type="button"
        aria-label="Open user menu"
        aria-expanded={open}
        onClick={toggleMenu}
        className="user-menu__trigger"
      >
        <Avatar name={user.displayName} tone={user.avatarTone ?? 'gold'} />
      </button>

      {open && (
        /* agent-forge-harness-3j4: no ARIA menu promise is made here any
           more — `role="group"` names the two actions without claiming a
           keyboard pattern (arrow keys, Home/End, roving tabindex) nothing
           implements. The role row (read-only status) and the sign-out
           error (an alert) sit beside the group, same as before 27h's
           aria-required-children fix — nothing moves on screen. */
        <div className="user-menu__popover">
          <div className="user-menu__item user-menu__role">
            <span id="user-menu-role-label">Dungeon Master</span>
            {/* Read-only (x5bz.2): role is server-authoritative from the
                signed session, not user-togglable. Disabled Switch keeps the
                same at-a-glance display without implying it's interactive. */}
            <Switch
              checked={user.role === 'dm'}
              disabled
              ariaLabel="Dungeon Master role"
            />
          </div>
          <div role="group" aria-label="User menu" className="user-menu__items">
            <button
              type="button"
              onClick={handleOpenProfile}
              className="user-menu__item"
            >
              Profile
            </button>
            <button
              type="button"
              onClick={handleSignOut}
              className="user-menu__item"
            >
              Sign out
            </button>
          </div>
          {signOutError && (
            <p role="alert" className="user-menu__item user-menu__error">
              {signOutError}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
