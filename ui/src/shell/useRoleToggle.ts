/**
 * useRoleToggle — the player/DM role switch with its channel-chats CP-D rule:
 * giving up the DM role while sitting in the GM channel drops the user back to
 * sage (GM is DM-only). Shared by the UserMenu switch and the ProfilePage role
 * control so the fallback lives in exactly one place.
 */

import { useCallback } from 'react'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import type { UserRole } from './currentUser'

/** Returns a `toggleRole(next)` where `next=true` means DM. */
export function useRoleToggle(): (next: boolean) => void {
  const { setRole } = useCurrentUser()
  const { mode, setMode } = useAppNav()

  return useCallback(
    (next: boolean) => {
      const role: UserRole = next ? 'dm' : 'player'
      setRole(role)
      if (role === 'player' && mode === 'gm') {
        setMode('sage')
      }
    },
    [setRole, mode, setMode],
  )
}
