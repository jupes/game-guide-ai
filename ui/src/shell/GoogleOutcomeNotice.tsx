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
import { clearGoogleOutcome, useGoogleOutcome, type GoogleOutcome } from './googleOutcome'
import './GoogleOutcomeNotice.css'

export interface GoogleOutcomeNoticeProps {
  scope?: 'any' | 'profile'
}

export function GoogleOutcomeNotice({ scope = 'any' }: GoogleOutcomeNoticeProps): React.JSX.Element | null {
  const pending = useGoogleOutcome()
  const [outcome, setOutcome] = React.useState<GoogleOutcome | null>(null)
  if (pending !== null && pending !== outcome && (scope === 'any' || pending.arrivedOn === 'profile')) {
    setOutcome(pending)
  }
  // The mounted notice remains readable after the shared store releases it.
  React.useEffect(() => {
    if (outcome !== null) clearGoogleOutcome(outcome)
  }, [outcome])
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
