import * as React from 'react'
import { useAppNav } from './shell/AppNav'
import { Landing } from './shell/Landing'
import { WorkspaceShell } from './shell/WorkspaceShell'
import { ProfilePage } from './shell/ProfilePage'
import { Login } from './shell/Login'
import { Signup } from './shell/Signup'
import { useCurrentUser } from './shell/currentUser'
import { getInviteTokenFromHash } from './shell/inviteToken'
import './shell/AuthScreen.css'

export default function App(): React.JSX.Element {
  const { screen, backToLanding, setConversationId } = useAppNav()
  const { authStatus, retryAuthCheck, user } = useCurrentUser()

  // Capture the invite ONCE, from the fragment (never sent to the server, so it
  // stays out of request logs). It's a single-use credential: clear it from the
  // address bar so it doesn't linger in history, and don't re-read it after
  // it's spent — otherwise signing out later lands the user on Signup with a
  // consumed token and no way forward.
  const [invite, setInvite] = React.useState<string | null>(() =>
    getInviteTokenFromHash(window.location.hash),
  )
  React.useEffect(() => {
    if (getInviteTokenFromHash(window.location.hash) !== null) {
      window.history.replaceState(
        {}, '', window.location.pathname + window.location.search,
      )
    }
  }, [])

  // Identity changed (sign-in, sign-out, session expiry): drop navigation state
  // so the incoming user never inherits the previous one's open conversation.
  React.useEffect(() => {
    setConversationId(null)
    backToLanding()
  }, [user.id, setConversationId, backToLanding])

  // Hold everything until the identity is known. Rendering the workspace during
  // the check let a returning user start a conversation while the store was
  // still scoped to `guest`; when their identity arrived the store switched and
  // that conversation's metadata was stranded (with no server-side listing to
  // recover it from). A brief neutral state is cheaper than losing data — and
  // it avoids flashing Login at an already-signed-in tester.
  if (authStatus === 'checking') {
    return (
      <div className="auth-screen" role="status" aria-live="polite">
        <p>Loading…</p>
      </div>
    )
  }

  // The check could not be completed (5xx, network failure). NOT a logout: the
  // session cookie may be perfectly valid, and Login would be a dead end anyway
  // because the same backend has to serve it. Say what happened and offer a
  // retry, so a passing outage costs a click rather than a credential re-entry.
  if (authStatus === 'unavailable') {
    return (
      <div className="auth-screen" role="alert">
        <h1>Can’t reach the service</h1>
        <p>
          We couldn’t check your session. This is usually temporary — your
          sign-in is probably still fine.
        </p>
        <button type="button" onClick={retryAuthCheck}>Try again</button>
      </div>
    )
  }

  // Definitively signed out: Signup while an unspent invite is in hand,
  // otherwise Login.
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
