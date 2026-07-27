import { describe, it, expect } from 'vitest'
import { getInviteTokenFromSearch } from './inviteToken'

describe('getInviteTokenFromSearch', () => {
  it('returns the invite token when present', () => {
    expect(getInviteTokenFromSearch('?invite=abc123')).toBe('abc123')
  })

  it('returns null when there is no invite param', () => {
    expect(getInviteTokenFromSearch('')).toBeNull()
    expect(getInviteTokenFromSearch('?other=1')).toBeNull()
  })

  it('returns null for an empty invite value', () => {
    expect(getInviteTokenFromSearch('?invite=')).toBeNull()
  })

  it('decodes a url-encoded token', () => {
    expect(getInviteTokenFromSearch('?invite=a-b_c%3D%3D')).toBe('a-b_c==')
  })

  it('ignores other params alongside invite', () => {
    expect(getInviteTokenFromSearch('?foo=bar&invite=xyz&baz=1')).toBe('xyz')
  })
})
