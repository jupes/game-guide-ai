import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  CurrentUserProvider,
  useCurrentUser,
} from './currentUser'

// ── 02t.6 — useCurrentUser provider guard (matches useTheme's pattern) ─────────

describe('useCurrentUser', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('throws a helpful error when used outside a <CurrentUserProvider>', () => {
    // React logs the thrown error to console.error during render — silence it.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useCurrentUser())).toThrow(/CurrentUserProvider/)
  })

  it('returns the provided user inside a <CurrentUserProvider>', () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <CurrentUserProvider>{children}</CurrentUserProvider>
    )
    const { result } = renderHook(() => useCurrentUser(), { wrapper })
    expect(result.current.user.displayName).toBe('Adventurer')
  })
})
