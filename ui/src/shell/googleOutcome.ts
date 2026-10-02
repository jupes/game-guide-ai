/**
 * googleOutcome -- what the Google callback said, as a sentence (lvs7).
 *
 * Every exit of `GET /auth/google/callback` (and of the refused starts) is a
 * 303 to `/` or `/profile` carrying `?google=<code>`, where `<code>` is a word
 * from a closed set the service owns (`service/google_oidc.py`, `Outcome`). This
 * module is the other half of that contract:
 *
 *  - the code is looked up in a FIXED table; a value the table does not hold
 *    gets the `failed` text, and the raw value is never rendered, stored or
 *    logged (it came out of the address bar, so it is attacker-shaped);
 *  - the parameter is read ONCE at boot and removed from the address bar with
 *    `replaceState`, keeping the path, every other query pair and the fragment
 *    byte for byte, so a reload or a copied URL does not replay the message;
 *  - it is a query parameter, not a fragment key, on purpose: the fragment
 *    grammar belongs to the workspace (`1kg.6.3`, CANVAS-30) and its reserved
 *    keys are single-use credentials. The code is not a credential.
 *
 * The outcome is held in a tiny module-level store so Login, Signup and the
 * Profile page can show it without prop-drilling through `App`.
 */

import { useSyncExternalStore } from 'react'

/** The closed set of codes the service can send. */
export const GOOGLE_OUTCOME_CODES = [
  'cancelled',
  'expired',
  'failed',
  'unavailable',
  'throttled',
  'no_account',
  'email_unverified',
  'invite_unusable',
  'invite_retry',
  'google_in_use',
  'email_in_use',
  'signin_required',
  'already_linked',
  'linked',
  'reauth_failed',
] as const

export type GoogleOutcomeCode = (typeof GOOGLE_OUTCOME_CODES)[number]

const MESSAGES: Readonly<Record<GoogleOutcomeCode, string>> = {
  cancelled: 'Google sign-in was cancelled.',
  expired: "That Google sign-in didn't complete. Please try again.",
  failed: "We couldn't sign you in with Google. Please try again.",
  unavailable:
    "Google sign-in isn't available right now. Please try again shortly, or use your password.",
  throttled: 'Too many attempts. Wait a few minutes, then try again.',
  no_account:
    "We couldn't find an account for that Google sign-in. If you were invited, open your invite link. If you already have an account, sign in with your password, then link Google from your profile.",
  email_unverified:
    "That Google account's email address isn't verified, so it can't be used here.",
  invite_unusable: "This invite can't be used. Ask the person who invited you for a new link.",
  invite_retry:
    "Signing up with Google didn't finish. Open your invite link again to try once more.",
  google_in_use:
    'That Google account is already linked to an account here. Sign in with Google instead.',
  email_in_use:
    "An account already uses that Google account's email address. Sign in with your password, then link Google from your profile.",
  signin_required: 'Sign in again, then link your Google account.',
  already_linked: 'Your account is already linked to a different Google account.',
  linked: 'Google account linked. You can now sign in with Google.',
  reauth_failed: "That password isn't right, so Google wasn't linked.",
}

export interface GoogleOutcome {
  /** A member of the closed set; an unknown value arrives as `failed`. */
  code: GoogleOutcomeCode
  /** The sentence to show. Plain text: render it as a React text child. */
  message: string
  /** `linked` is good news (a polite status); every other code is an alert. */
  role: 'alert' | 'status'
  /** Which page the server sent the browser to. The link flow lands on
   * `/profile`; every other flow lands on `/`. */
  arrivedOn: 'home' | 'profile'
}

const PARAM = 'google'

function isCode(value: string): value is GoogleOutcomeCode {
  return (GOOGLE_OUTCOME_CODES as readonly string[]).includes(value)
}

function pairName(pair: string): string {
  return new URLSearchParams(pair).keys().next().value ?? ''
}

/** The outcome in a query string, or `null` when it carries no `google` pair.
 * A present-but-unrecognised value is the `failed` outcome, not `null`. */
export function readGoogleOutcome(search: string, pathname: string): GoogleOutcome | null {
  const raw = search.startsWith('?') ? search.slice(1) : search
  const params = new URLSearchParams(raw)
  if (!params.has(PARAM)) return null
  const value = params.get(PARAM) ?? ''
  const code: GoogleOutcomeCode = isCode(value) ? value : 'failed'
  return {
    code,
    message: MESSAGES[code],
    role: code === 'linked' ? 'status' : 'alert',
    arrivedOn: pathname === '/profile' ? 'profile' : 'home',
  }
}

/** A query string without its `google` pair(s). Filters the raw `&`-separated
 * pairs rather than round-tripping through URLSearchParams, which would rewrite
 * pairs it has no business touching (`section` -> `section=`, `%20` -> `+`).
 * Each pair's NAME is still decoded, so `goo%67le` is recognised. Returns the
 * remainder WITH its leading `?`, or `''` when nothing is left. */
export function scrubGoogleParam(search: string): string {
  const raw = search.startsWith('?') ? search.slice(1) : search
  if (raw === '') return ''
  const kept = raw.split('&').filter((pair) => pairName(pair) !== PARAM)
  const rest = kept.join('&')
  return rest === '' ? '' : `?${rest}`
}

// ── The store ────────────────────────────────────────────────────────────────

let current: GoogleOutcome | null = null
const listeners = new Set<() => void>()

function publish(next: GoogleOutcome | null): void {
  current = next
  for (const listener of listeners) listener()
}

/** The held outcome, if any. */
export function getGoogleOutcome(): GoogleOutcome | null {
  return current
}

/** Hold the outcome a location carries. Does not touch the address bar. A
 * location with no `google` pair leaves whatever is held alone. */
export function setGoogleOutcome(location: { search: string; pathname: string }): void {
  const outcome = readGoogleOutcome(location.search, location.pathname)
  if (outcome !== null) publish(outcome)
}

/** Forget the held outcome. */
export function clearGoogleOutcome(): void {
  if (current !== null) publish(null)
}

/** Boot-only: read the callback's code from the address bar, hold it, and
 * remove it from the bar (path, other pairs and fragment untouched). Safe to
 * call twice -- the second call finds the bar already clean and keeps what the
 * first one read. */
export function captureGoogleOutcome(): void {
  const { search, pathname, hash } = window.location
  if (readGoogleOutcome(search, pathname) === null) return
  setGoogleOutcome({ search, pathname })
  window.history.replaceState({}, '', pathname + scrubGoogleParam(search) + hash)
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** The held outcome, re-rendering when it changes. */
export function useGoogleOutcome(): GoogleOutcome | null {
  return useSyncExternalStore(subscribe, getGoogleOutcome, getGoogleOutcome)
}
