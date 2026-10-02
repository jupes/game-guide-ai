/**
 * googleOutcome (lvs7 pr-b) -- the closed table that turns the `?google=<code>`
 * the callback redirects with into a sentence. The code is a word from a fixed
 * set; anything else is the `failed` text, and the raw value is never rendered.
 */

import { afterEach, describe, expect, it } from 'vitest'
import {
  GOOGLE_OUTCOME_CODES,
  captureGoogleOutcome,
  clearGoogleOutcome,
  getGoogleOutcome,
  readGoogleOutcome,
  scrubGoogleParam,
} from './googleOutcome'

const FAILED = "We couldn't sign you in with Google. Please try again."

const TABLE: Record<string, string> = {
  cancelled: 'Google sign-in was cancelled.',
  expired: "That Google sign-in didn't complete. Please try again.",
  failed: FAILED,
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

afterEach(() => {
  clearGoogleOutcome()
  window.history.replaceState({}, '', '/')
})

describe('readGoogleOutcome', () => {
  it('knows exactly the codes the server can send', () => {
    expect([...GOOGLE_OUTCOME_CODES].sort()).toEqual(Object.keys(TABLE).sort())
  })

  for (const [code, message] of Object.entries(TABLE)) {
    it(`${code} -> its exact message`, () => {
      const outcome = readGoogleOutcome(`?google=${code}`, '/')
      expect(outcome?.message).toBe(message)
      expect(outcome?.code).toBe(code)
    })
  }

  it('only `linked` is a status; every other code is an alert', () => {
    expect(readGoogleOutcome('?google=linked', '/profile')?.role).toBe('status')
    for (const code of Object.keys(TABLE).filter((c) => c !== 'linked')) {
      expect(readGoogleOutcome(`?google=${code}`, '/')?.role, code).toBe('alert')
    }
  })

  it('an unknown or markup-shaped value gets the failed text, never the raw value', () => {
    const raws = [
      'nope', '<img src=x onerror=alert(1)>', 'FAILED', 'failed ', '',
      'constructor', '__proto__', 'toString',
    ]
    for (const raw of raws) {
      const outcome = readGoogleOutcome(`?google=${encodeURIComponent(raw)}`, '/')
      expect(outcome, raw).not.toBeNull()
      expect(outcome?.message, raw).toBe(FAILED)
      expect(outcome?.code, raw).toBe('failed')
      expect(JSON.stringify(outcome)).not.toContain('onerror')
    }
  })

  it('is null when there is no google parameter', () => {
    expect(readGoogleOutcome('', '/')).toBeNull()
    expect(readGoogleOutcome('?q=1', '/')).toBeNull()
    expect(readGoogleOutcome('?googled=failed', '/')).toBeNull()
  })

  it('takes the first value when the parameter repeats', () => {
    expect(readGoogleOutcome('?google=linked&google=failed', '/profile')?.code).toBe('linked')
  })

  it('records whether it arrived on the profile page', () => {
    expect(readGoogleOutcome('?google=linked', '/profile')?.arrivedOn).toBe('profile')
    expect(readGoogleOutcome('?google=failed', '/')?.arrivedOn).toBe('home')
    expect(readGoogleOutcome('?google=failed', '/anything-else')?.arrivedOn).toBe('home')
  })
})

describe('scrubGoogleParam', () => {
  it('drops only the google pair and leaves every other pair byte for byte', () => {
    expect(scrubGoogleParam('?google=failed')).toBe('')
    expect(scrubGoogleParam('?a=1&google=failed&b=%20x')).toBe('?a=1&b=%20x')
    expect(scrubGoogleParam('?section&google=linked')).toBe('?section')
    expect(scrubGoogleParam('?q=1')).toBe('?q=1')
    expect(scrubGoogleParam('')).toBe('')
  })

  it('drops an encoded name and every repeat', () => {
    expect(scrubGoogleParam('?goo%67le=failed&google=linked&k=v')).toBe('?k=v')
  })

  it('does not drop a parameter that merely contains the word', () => {
    expect(scrubGoogleParam('?googled=1&xgoogle=2')).toBe('?googled=1&xgoogle=2')
  })
})

describe('captureGoogleOutcome', () => {
  it('reads the query once, holds the outcome, and scrubs it keeping path and fragment', () => {
    window.history.replaceState({}, '', '/profile?google=linked&k=v#section=a&x=%20y')
    captureGoogleOutcome()
    expect(getGoogleOutcome()?.code).toBe('linked')
    expect(getGoogleOutcome()?.arrivedOn).toBe('profile')
    expect(window.location.pathname).toBe('/profile')
    expect(window.location.search).toBe('?k=v')
    expect(window.location.hash).toBe('#section=a&x=%20y')
  })

  it('a second call (a strict-mode re-run) does not forget the first read', () => {
    window.history.replaceState({}, '', '/?google=no_account')
    captureGoogleOutcome()
    captureGoogleOutcome()
    expect(getGoogleOutcome()?.code).toBe('no_account')
    expect(window.location.search).toBe('')
  })

  it('does nothing, and writes nothing, when there is no google parameter', () => {
    window.history.replaceState({}, '', '/?q=1#a=2')
    captureGoogleOutcome()
    expect(getGoogleOutcome()).toBeNull()
    expect(window.location.search).toBe('?q=1')
    expect(window.location.hash).toBe('#a=2')
  })
})
