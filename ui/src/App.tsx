import * as React from 'react'
import { useAppNav } from './shell/AppNav'
import { Landing } from './shell/Landing'
import { WorkspaceShell } from './shell/WorkspaceShell'
import { ProfilePage } from './shell/ProfilePage'
import { Login } from './shell/Login'
import { Signup } from './shell/Signup'
import { useCurrentUser } from './shell/currentUser'
import { getInviteTokenFromSearch } from './shell/inviteToken'

export default function App(): React.JSX.Element {
  const { screen } = useAppNav()
  const { authStatus } = useCurrentUser()

  // Definitively signed out: gate on Login, or Signup if this is an invite
  // link (root-path `/?invite=<token>` — there is no client router, and the
  // built SPA 404s on any other path). While `checking`, fall through to the
  // normal screen below rather than flashing Login first — most visits are an
  // already-signed-in tester, and the screen swaps to Login the moment the
  // session check comes back negative.
  if (authStatus === 'unauthenticated') {
    const invite = getInviteTokenFromSearch(window.location.search)
    return invite ? <Signup invite={invite} /> : <Login />
  }

  if (screen === 'landing') return <Landing />
  if (screen === 'profile') return <ProfilePage />
  return <WorkspaceShell />
}
