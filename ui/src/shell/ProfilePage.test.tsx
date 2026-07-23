/**
 * ProfilePage (swe1.7) — render + edit + navigation.
 *
 * Driven through a stateful harness so edits flow through the real setters
 * (name/tone/role) and the avatar re-derives, exercising the wiring end to end.
 */

import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState, ChatMode } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue, UserRole } from './currentUser'
import { deriveInitials, type AvatarTone } from '../ds/Avatar'
import { ProfilePage } from './ProfilePage'

interface HarnessProps {
  initialRole?: UserRole
  mode?: ChatMode
  setMode?: (m: ChatMode) => void
  backToWorkspace?: () => void
}

/** ProfilePage wired to real name/tone/role state + spyable nav actions. */
function Harness({
  initialRole = 'player',
  mode = 'sage',
  setMode = vi.fn(),
  backToWorkspace = vi.fn(),
}: HarnessProps) {
  const [displayName, setDisplayName] = useState('Adventurer')
  const [avatarTone, setAvatarTone] = useState<AvatarTone>('gold')
  const [role, setRole] = useState<UserRole>(initialRole)

  const userValue: CurrentUserContextValue = {
    user: {
      id: 'guest',
      displayName,
      initials: deriveInitials(displayName),
      avatarTone,
      role,
      signOut: vi.fn(),
      editProfile: vi.fn(),
    },
    setRole,
    setDisplayName,
    setAvatarTone,
  }

  const navValue: AppNavState = {
    screen: 'profile',
    mode,
    conversationId: null,
    enterWorkspace: vi.fn(),
    setMode,
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    openProfile: vi.fn(),
    backToWorkspace,
  }

  return (
    <AppNavContext.Provider value={navValue}>
      <CurrentUserContext.Provider value={userValue}>
        <ProfilePage />
      </CurrentUserContext.Provider>
    </AppNavContext.Provider>
  )
}

describe('ProfilePage (swe1.7)', () => {
  it('shows the current name, avatar, and role', () => {
    render(<Harness />)
    expect(screen.getByRole('heading', { name: 'Profile' })).toBeInTheDocument()
    expect(screen.getByRole('textbox')).toHaveValue('Adventurer')
    // Not a DM by default → role switch is off.
    expect(screen.getByRole('switch', { name: /dungeon master/i })).not.toBeChecked()
    // Avatars render the derived initials ('Adventurer' → 'A').
    expect(screen.getAllByText('A').length).toBeGreaterThan(0)
  })

  it('editing the name updates it and the avatar re-derives its initials', async () => {
    render(<Harness />)
    const input = screen.getByRole('textbox')
    await userEvent.clear(input)
    await userEvent.type(input, 'Astra Vail')
    expect(input).toHaveValue('Astra Vail')
    // Initials update: 'Astra Vail' → 'AV' (absent before the rename).
    expect(screen.getAllByText('AV').length).toBeGreaterThan(0)
  })

  it('choosing an avatar tone selects it', async () => {
    render(<Harness />)
    const arcane = screen.getByRole('button', { name: /arcane avatar/i })
    expect(arcane).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(arcane)
    expect(arcane).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /gold avatar/i })).toHaveAttribute('aria-pressed', 'false')
  })

  it('toggling off the DM role in the GM channel falls back to sage', async () => {
    const setMode = vi.fn()
    render(<Harness initialRole="dm" mode="gm" setMode={setMode} />)
    await userEvent.click(screen.getByRole('switch', { name: /dungeon master/i }))
    expect(setMode).toHaveBeenCalledWith('sage')
  })

  it('Back to chat returns to the workspace', async () => {
    const backToWorkspace = vi.fn()
    render(<Harness backToWorkspace={backToWorkspace} />)
    await userEvent.click(screen.getByRole('button', { name: /back to chat/i }))
    expect(backToWorkspace).toHaveBeenCalledTimes(1)
  })

  it('documents the fields that await a real account', () => {
    render(<Harness />)
    const note = screen.getByLabelText('Awaiting sign-in')
    expect(within(note).getByText(/available once you have an account/i)).toBeInTheDocument()
    expect(within(note).getByText(/username/i)).toBeInTheDocument()
  })
})
