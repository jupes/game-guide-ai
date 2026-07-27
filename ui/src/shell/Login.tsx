/**
 * Login — email + password sign-in (x5bz.2).
 *
 * Rendered by App when the session check comes back unauthenticated and the
 * URL carries no invite token. On success, adopts the session via
 * useCurrentUser().signIn and resets the screen to landing.
 */

import * as React from 'react'
import { useState } from 'react'
import { Button } from '../ds/Button'
import { Card } from '../ds/Card'
import { TextField } from '../ds/TextField'
import * as api from '../api'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import './AuthScreen.css'

export function Login(): React.JSX.Element {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const { signIn } = useCurrentUser()
  const { backToLanding } = useAppNav()

  async function handleSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    const result = await api.login(email, password)
    setSubmitting(false)
    if (result.kind === 'ok') {
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
        <p className="auth-screen__tagline">Sign in to continue</p>
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
