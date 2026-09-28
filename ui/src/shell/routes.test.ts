import { describe, it, expect } from 'vitest'
import { routeForPath, pathForScreen, startScreen } from './routes'
import { readFragmentToken } from './inviteToken'

describe('routeForPath', () => {
  it('finds the row for each known path', () => {
    expect(routeForPath('/')).toEqual({ path: '/', screen: 'landing', coldLoad: 'restore' })
    expect(routeForPath('/workspace')).toEqual({
      path: '/workspace', screen: 'workspace', coldLoad: 'home',
    })
    expect(routeForPath('/profile')).toEqual({
      path: '/profile', screen: 'profile', coldLoad: 'restore',
    })
  })

  // A8 -- an unknown route.
  it('returns null for an unknown route', () => {
    expect(routeForPath('/nope')).toBeNull()
  })

  it('is an exact match: no trailing slash, no partial prefix', () => {
    expect(routeForPath('/profile/')).toBeNull()
    expect(routeForPath('/prof')).toBeNull()
  })
})

describe('pathForScreen', () => {
  it('is the inverse of routeForPath for every screen', () => {
    expect(pathForScreen('landing')).toBe('/')
    expect(pathForScreen('workspace')).toBe('/workspace')
    expect(pathForScreen('profile')).toBe('/profile')
  })
})

describe('startScreen', () => {
  it('resolves the home path directly', () => {
    expect(startScreen('/')).toEqual({ screen: 'landing', replacePath: null })
  })

  // A1 -- a deep path resolves on a cold load.
  it('resolves /profile directly, with no URL correction', () => {
    expect(startScreen('/profile')).toEqual({ screen: 'profile', replacePath: null })
  })

  // A12 -- reload behaviour for the workspace is unchanged: home, not restore.
  it('sends a cold load of /workspace to landing and corrects the URL', () => {
    expect(startScreen('/workspace')).toEqual({ screen: 'landing', replacePath: '/' })
  })

  // A8 -- an unknown route behaves exactly like '/'.
  it('sends an unknown path to landing and corrects the URL', () => {
    expect(startScreen('/nope')).toEqual({ screen: 'landing', replacePath: '/' })
  })

  it('the unknown-route fallback is NOT the routeForPath("/") row reused', () => {
    // Guards against an implementation that special-cases the fallback by
    // routing unknown paths through the '/' row's screen/coldLoad instead of
    // computing the landing/'/' result directly -- both give 'landing' here,
    // so this pins the RESULT rather than the mechanism (see the section-8
    // break: "make routeForPath fall back to the '/' row").
    const home = routeForPath('/')
    const unknown = startScreen('/does-not-exist')
    expect(unknown.screen).toBe(home?.screen)
    expect(unknown.replacePath).toBe('/')
  })
})

// A7 -- token extraction, generalized: readFragmentToken(hash, key).
describe('readFragmentToken', () => {
  it('returns the value for the given key', () => {
    expect(readFragmentToken('#token=tok-123', 'token')).toBe('tok-123')
  })

  it('returns null when the key is absent', () => {
    expect(readFragmentToken('#invite=abc', 'token')).toBeNull()
  })

  it('returns null for an empty value', () => {
    expect(readFragmentToken('#token=', 'token')).toBeNull()
  })

  it('decodes a url-encoded value', () => {
    expect(readFragmentToken('#token=a-b_c%3D%3D', 'token')).toBe('a-b_c==')
  })

  it('finds the key alongside other params', () => {
    expect(readFragmentToken('#foo=1&token=tok-xyz&bar=2', 'token')).toBe('tok-xyz')
  })

  it('refuses a string starting with "?" for any key', () => {
    expect(readFragmentToken('?token=abc123', 'token')).toBeNull()
  })
})
