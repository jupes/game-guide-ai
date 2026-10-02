/**
 * GoogleSignInButton -- the "Sign in with Google" control (lvs7).
 *
 * No Google script runs on our pages (so the CSP and COOP stay exactly as they
 * are): signing in is a NAVIGATION to our own server, which does the OIDC dance.
 *
 *  - `signin`  an `<a href="/auth/google/start">` -- a plain top-level GET.
 *  - `signup`  a submit button; the parent `<form method="post">` carries the
 *              invite in the BODY. A token in a URL would reach the request log.
 *  - `link`    a submit button in the Profile form, which also carries the
 *              account's current password.
 *
 * Look and wording follow Google's branding guidelines
 * (developers.google.com/identity/branding-guidelines, read 2026-10-01): the
 * stock four-colour "G", unmodified, at the left; one of the three sanctioned
 * labels; 14/20 medium; 12px | logo | 10px | label | 12px. The colours are
 * Google's own light and dark buttons, carried as `--aether-google-btn-*`
 * tokens, and follow the app theme. The logo is decorative (`aria-hidden`), so
 * the accessible name is the label alone.
 */

import * as React from 'react'
import './GoogleSignInButton.css'

export type GoogleButtonKind = 'signin' | 'signup' | 'link'

const LABELS: Readonly<Record<GoogleButtonKind, string>> = {
  signin: 'Sign in with Google',
  signup: 'Sign up with Google',
  link: 'Continue with Google',
}

export const GOOGLE_START_PATH = '/auth/google/start'

export interface GoogleSignInButtonProps {
  kind: GoogleButtonKind
}

/** Google's standard "G", as published in their brand assets. Do not recolour. */
function GoogleLogo(): React.JSX.Element {
  return (
    <svg
      className="google-btn__logo"
      viewBox="0 0 48 48"
      width="20"
      height="20"
      aria-hidden="true"
      focusable="false"
    >
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  )
}

export function GoogleSignInButton({ kind }: GoogleSignInButtonProps): React.JSX.Element {
  const content = (
    <>
      <GoogleLogo />
      <span className="google-btn__label">{LABELS[kind]}</span>
    </>
  )
  if (kind === 'signin') {
    return (
      <a className="google-btn" href={GOOGLE_START_PATH}>
        {content}
      </a>
    )
  }
  return (
    <button className="google-btn" type="submit">
      {content}
    </button>
  )
}
