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
  const { screen, backToLanding, setConversationId } = useAppNav()
  const { authStatus, user } = useCurrentUser()

  // Capture the invite ONCE. It's a single-use credential, so it must not
  // linger in the URL (browser history, referrers, server logs) or be re-read
  // after it's spent — otherwise signing out later lands the user on Signup
  // with a consumed token and no way forward.
  const [invite, setInvite] = React.useState<string | null>(() =>
    getInviteTokenFromSearch(window.location.search),
  )
  React.useEffect(() => {
    if (getInviteTokenFromSearch(window.location.search) !== null) {
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  // Identity changed (sign-in, sign-out, session expiry): drop navigation state
  // so the incoming user never inherits the previous one's open conversation.
  React.useEffect(() => {
    setConversationId(null)
    backToLanding()
  }, [user.id, setConversationId, backToLanding])

  // Definitively signed out: Signup while an unspent invite is in hand,
  // otherwise Login. While `checking`, fall through to the normal screen rather
  // than flashing Login — most visits are an already-signed-in tester, and the
  // screen swaps the moment the session check comes back negative.
  if (authStatus === 'unauthenticated') {
    return invite ? (
      <Signup invite={invite} onUseLogin={() => setInvite(null)} />
    ) : (
      <Login />
    )
  }

  if (screen === 'landing') return <Landing />
  if (screen === 'profile') return <ProfilePage />
  return <WorkspaceShell />
}
