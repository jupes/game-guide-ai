/**
 * ProfileGoogleSection -- the "Google account" block on the Profile page (lvs7).
 *
 * Drawn ONLY when the service offers Google sign-in AND it has told us this
 * account's link status; if either answer is missing (the feature is off, signed
 * out, a 5xx), nothing is drawn -- "unknown" is never shown as "not linked".
 *
 *  - linked            "Linked to <address>". No unlink control: removing a
 *                      sign-in method is its own re-authenticated flow, not here.
 *  - linked, no password   also says the account signs in with Google.
 *  - not linked        a native form POST (no script) with the account's CURRENT
 *                      PASSWORD: linking a sign-in method is as sensitive as the
 *                      actions that already ask for it (SEC-40), so a stolen
 *                      session alone cannot do it. The password goes in the
 *                      request body only -- it is never held in React state.
 */

import * as React from 'react'
import * as api from '../api'
import type { GoogleLinkStatus } from '../api'
import { GoogleSignInButton, GOOGLE_START_PATH } from './GoogleSignInButton'
import { useGoogleAvailable } from './googleAvailability'
import './ProfilePage.css'

export function ProfileGoogleSection(): React.JSX.Element | null {
  const available = useGoogleAvailable()
  const [status, setStatus] = React.useState<GoogleLinkStatus | null>(null)
  const passwordId = React.useId()
  const hintId = React.useId()

  React.useEffect(() => {
    if (!available) return
    let live = true
    void api.getGoogleLink().then((answer) => {
      if (live) setStatus(answer)
    })
    return () => {
      live = false
    }
  }, [available])

  if (!available || status === null) return null
  // Not linked and no password cannot happen (a Google-only account is linked by
  // construction); if it ever does there is nothing safe to offer.
  if (!status.linked && !status.hasPassword) return null

  return (
    <section className="profile-page__google" aria-labelledby="profile-google-title">
      <h2 id="profile-google-title" className="profile-page__section-title">
        Google account
      </h2>
      {status.linked ? (
        <>
          <p className="profile-page__google-line">Linked to {status.email ?? 'your Google account'}</p>
          {!status.hasPassword && <p className="profile-page__google-line">You sign in with Google.</p>}
        </>
      ) : (
        <form method="post" action={GOOGLE_START_PATH} className="profile-page__google-form">
          <input type="hidden" name="intent" value="link" />
          <label htmlFor={passwordId} className="profile-page__field-label">
            Current password
          </label>
          <input
            id={passwordId}
            className="profile-page__password"
            type="password"
            name="password"
            autoComplete="current-password"
            aria-describedby={hintId}
            required
          />
          <p id={hintId} className="profile-page__google-hint">
            We ask for your password so that nobody else can add a Google account to yours.
          </p>
          <GoogleSignInButton kind="link" />
        </form>
      )}
    </section>
  )
}
