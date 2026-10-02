/**
 * GoogleOutcomeNotice -- says what the Google callback said (lvs7).
 *
 * Reads the held outcome (googleOutcome.ts) and renders its fixed sentence as
 * text: `role="alert"` for a refusal, `role="status"` for a successful link.
 * `scope="profile"` shows only an outcome the server sent to /profile, so a
 * refusal that belonged to the sign-in screen is not replayed there after the
 * person signs in some other way.
 */

import * as React from 'react'
import { useGoogleOutcome } from './googleOutcome'
import './GoogleOutcomeNotice.css'

export interface GoogleOutcomeNoticeProps {
  scope?: 'any' | 'profile'
}

export function GoogleOutcomeNotice({ scope = 'any' }: GoogleOutcomeNoticeProps): React.JSX.Element | null {
  const outcome = useGoogleOutcome()
  if (outcome === null) return null
  if (scope === 'profile' && outcome.arrivedOn !== 'profile') return null
  return (
    <p
      role={outcome.role}
      className={
        outcome.role === 'alert'
          ? 'google-outcome google-outcome--error'
          : 'google-outcome google-outcome--ok'
      }
    >
      {outcome.message}
    </p>
  )
}
