/**
 * Signup — redeem a one-time invite to create an account (x5bz.2).
 *
 * Rendered by App when the URL is an invite deep-link (`/#invite=<token>`,
 * the root path — there is no client router). The invite token itself is
 * opaque to the tester; only email + password are entered.
 */

import * as React from 'react'
import { useState } from 'react'
import { Button } from '../ds/Button'
import { Card } from '../ds/Card'
import { TextField } from '../ds/TextField'
import * as api from '../api'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { validateCredentials } from './credentials'
import './AuthScreen.css'

export interface SignupProps {
  invite: string
  /** Discard the invite and show Login instead (also called once it's spent). */
  onUseLogin: () => void
}

export function Signup({ invite, onUseLogin }: SignupProps): React.JSX.Element {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const { signIn } = useCurrentUser()
  const { backToLanding } = useAppNav()

  async function handleSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault()
    const invalid = validateCredentials(email, password)
    if (invalid) {
      setError(invalid)
      return
    }
    setError(null)
    setSubmitting(true)
    const result = await api.signup(email, password, invite)
    setSubmitting(false)
    if (result.kind === 'ok') {
      onUseLogin() // the invite is spent — never offer it again
      signIn(result.user)
      backToLanding()
    } else {
      setError(result.message)
    }
  }

  return (
    <div className="auth-screen">
      <Card className="auth-screen__card">
        <h1 className="auth-screen__title">Aetheril</h1>
        <p className="auth-screen__tagline">You've been invited — create your account</p>
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
            supportingText="At least 8 characters"
            fullWidth
          />
          {error && (
            <p role="alert" className="auth-screen__error">
              {error}
            </p>
          )}
          <Button type="submit" variant="filled" fullWidth disabled={submitting}>
            {submitting ? 'Creating account…' : 'Create account'}
          </Button>
        </form>
        <p className="auth-screen__switch">
          <Button variant="text" onClick={onUseLogin}>
            Already have an account? Sign in
          </Button>
        </p>
      </Card>
    </div>
  )
}
