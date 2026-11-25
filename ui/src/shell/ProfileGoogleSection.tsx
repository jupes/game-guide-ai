/**
 * ProfileGoogleSection -- the "Google account" block on the Profile page (lvs7).
 *
 * Drawn ONLY when the service offers Google sign-in AND it has told us this
 * account's link status; if either answer is missing (the feature is off, signed
 * out, a 5xx), nothing is drawn -- "unknown" is never shown as "not linked".
 *
 * Linking and unlinking both use a native form with the current password.
 * A Google-only account needs its linked identity to sign in.
 */

import * as React from 'react'
import * as api from '../api'
import type { GoogleLinkStatus } from '../api'
import { Button } from '../ds/Button'
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
      {status.linked && <p className="profile-page__google-line">Linked to {status.email ?? 'your Google account'}</p>}
      {!status.hasPassword && <p className="profile-page__google-line">You sign in with Google.</p>}
      {status.hasPassword && (
        <form
          method="post"
          action={status.linked ? '/auth/google/unlink' : GOOGLE_START_PATH}
          className="profile-page__google-form"
        >
          {!status.linked && <input type="hidden" name="intent" value="link" />}
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
            {status.linked
              ? 'After unlinking, use your password to sign in.'
              : 'We ask for your password so that nobody else can add a Google account to yours.'}
          </p>
          {status.linked
            ? <Button variant="outlined" type="submit">Unlink Google</Button>
            : <GoogleSignInButton kind="link" />}
        </form>
      )}
    </section>
  )
}
