import * as React from 'react'
import { useAppNav } from './shell/AppNav'
import { Landing } from './shell/Landing'
import { WorkspaceShell } from './shell/WorkspaceShell'
import { ProfilePage } from './shell/ProfilePage'
import { TavernScreen } from './shell/TavernScreen'
import { Login } from './shell/Login'
import { Signup } from './shell/Signup'
import { STUB, useCurrentUser } from './shell/currentUser'
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
  //
  // `user.id` alone can't tell this apart from the FIRST resolution of the
  // session check: `checking -> authenticated` and `unauthenticated ->
  // authenticated` are both 'guest' -> <email> (currentUser.tsx). Naively
  // resetting on every `user.id` change would clobber a cold-loaded deep
  // link the moment the session check resolves (agent-forge-harness-y40,
  // R8) — so this only fires between two SETTLED statuses (`authenticated`
  // or `unauthenticated`; `checking`/`unavailable` haven't answered the
  // question yet) whose ids actually differ. `lastSettledUserId` remembers
  // the last settled id seen, `null` meaning "none yet".
  //
  // One exception (30c PR-2, ID-26, "sign-in returns you where you were"): a
  // cold load of the signed-out `/tavern`, signed in from the Login or Signup it
  // showed, stays on the tavern instead of dropping to Landing. It is the path
  // alone that returns: the address bar is rewritten without any fragment, so
  // nothing in it is read as an instruction. Every other transition -- sign-out,
  // an expired session, an account switch, a sign-in over any other screen --
  // still resets to Landing.
  const lastSettledUserId = React.useRef<string | null>(null)
  React.useEffect(() => {
    const settled = authStatus === 'authenticated' || authStatus === 'unauthenticated'
    if (!settled) return
    const previous = lastSettledUserId.current
    if (previous !== null && previous !== user.id) {
      setConversationId(null)
      if (previous === STUB.id && authStatus === 'authenticated' && screen === 'tavern') {
        if (window.location.hash !== '') {
          window.history.replaceState({}, '', window.location.pathname + window.location.search)
        }
      } else {
        // `replace`, not a user navigation: this must not leave a history
        // entry a signed-out user (or the next account) could Back into.
        backToLanding('replace')
      }
    }
    lastSettledUserId.current = user.id
  }, [authStatus, user.id, screen, setConversationId, backToLanding])

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
        <button type="button" className="auth-screen__retry" onClick={retryAuthCheck}>
          Try again
        </button>
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
  if (screen === 'tavern') return <TavernScreen />
  return <WorkspaceShell />
}
