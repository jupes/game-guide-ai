import { describe, it, expect } from 'vitest'
import { getInviteTokenFromHash } from './inviteToken'

describe('getInviteTokenFromHash', () => {
  it('returns the invite token when present', () => {
    expect(getInviteTokenFromHash('#invite=abc123')).toBe('abc123')
  })

  it('tolerates a missing leading #', () => {
    expect(getInviteTokenFromHash('invite=abc123')).toBe('abc123')
  })

  it('returns null when there is no invite fragment', () => {
    expect(getInviteTokenFromHash('')).toBeNull()
    expect(getInviteTokenFromHash('#')).toBeNull()
    expect(getInviteTokenFromHash('#other=1')).toBeNull()
  })

  it('returns null for an empty invite value', () => {
    expect(getInviteTokenFromHash('#invite=')).toBeNull()
  })

  it('decodes a url-encoded token', () => {
    expect(getInviteTokenFromHash('#invite=a-b_c%3D%3D')).toBe('a-b_c==')
  })

  it('ignores other fragment params alongside invite', () => {
    expect(getInviteTokenFromHash('#foo=bar&invite=xyz&baz=1')).toBe('xyz')
  })

  it('does NOT read a token from the query string', () => {
    // The token must travel in the fragment only: a query string reaches the
    // server and lands in Cloud Run's request logs.
    expect(getInviteTokenFromHash('?invite=abc123')).toBeNull()
  })
})
