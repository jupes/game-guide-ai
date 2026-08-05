/**
 * Client-side credential checks (x5bz.2).
 *
 * Mirrors the server's rules so ordinary mistakes (empty email, 7-character
 * password) are caught before a round trip. This is a UX nicety, NOT the
 * security boundary — the service validates independently and is what actually
 * rejects bad input.
 */

export const MIN_PASSWORD_LENGTH = 8

export function validateCredentials(email: string, password: string): string | null {
  if (!email.trim()) return 'Enter your email address.'
  if (!email.includes('@')) return 'Enter a valid email address.'
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`
  }
  return null
}
