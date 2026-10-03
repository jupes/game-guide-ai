/**
 * Login — email + password sign-in (x5bz.2).
 *
 * Rendered by App when the session check comes back unauthenticated and the
 * URL carries no invite token. On success, adopts the session via
 * useCurrentUser().signIn and resets the screen to landing -- except over the
 * tavern, which a cold load of the signed-out /tavern returns to (30c PR-2).
 *
 * A 401 during a live reveal (agent-forge-harness-1kg.7.3 PR-2, REVEAL-16) says what the table
 * can still see, from a notice held in memory until the GM has signed in again.
 */

import * as React from 'react'
import { useState } from 'react'
import { Button } from '../ds/Button'
import { Card } from '../ds/Card'
import { TextField } from '../ds/TextField'
import * as api from '../api'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { GoogleOutcomeNotice } from './GoogleOutcomeNotice'
import { GoogleSignInButton } from './GoogleSignInButton'
import { useGoogleAvailable } from './googleAvailability'
import { revealSignOutNotice, useRevealSignOutNotice } from './revealSignOut'
import './AuthScreen.css'

export function Login(): React.JSX.Element {
  const googleAvailable = useGoogleAvailable()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const { signIn } = useCurrentUser()
  const { screen, backToLanding } = useAppNav()
  const stillSees = useRevealSignOutNotice()

  async function handleSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault()
    if (!email.trim() || !password) {
      setError('Enter your email and password.')
      return
    }
    setError(null)
    setSubmitting(true)
    const result = await api.login(email, password)
    setSubmitting(false)
    if (result.kind === 'ok') {
      revealSignOutNotice.clear()
      signIn(result.user)
      // 30c PR-2 (ID-26): a sign-in over a cold-loaded /tavern stays there; every
      // other screen resets to Landing as before.
      if (screen !== 'tavern') backToLanding()
    } else {
      setError(result.message)
    }
  }

  return (
    <div className="auth-screen">
      <Card className="auth-screen__card">
        <h1 className="auth-screen__title">Aetheril</h1>
        <p className="auth-screen__tagline">Sign in to continue</p>
        {stillSees !== null && (
          <p role="alert" className="auth-screen__notice">
            {stillSees}
          </p>
        )}
        <GoogleOutcomeNotice />
        {googleAvailable && (
          <>
            <GoogleSignInButton kind="signin" />
            <p className="auth-screen__or">or</p>
          </>
        )}
        <form onSubmit={handleSubmit} className="auth-screen__form">
          <TextField
            label="Email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            fullWidth
          />
          <TextField
            label="Password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            fullWidth
          />
          {error && (
            <p role="alert" className="auth-screen__error">
              {error}
            </p>
          )}
          <Button type="submit" variant="filled" fullWidth disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
      </Card>
    </div>
  )
}
