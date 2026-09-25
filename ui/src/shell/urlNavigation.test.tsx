import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavProvider, useAppNav } from './AppNav'
import { UrlNavigation } from './UrlNavigation'
import { startScreen } from './routes'

/** Minimal consumer: renders the active screen as text (queryable by role,
 * with no dependency on any real screen component) and exposes the three
 * transitions UrlNavigation needs to reflect Back/Forward. */
function Screens(): React.JSX.Element {
  const { screen: active, enterWorkspace, openProfile, backToLanding } = useAppNav()
  return (
    <div>
      <p role="status">{active}</p>
      <button type="button" onClick={() => enterWorkspace()}>Enter workspace</button>
      <button type="button" onClick={() => openProfile()}>Open profile</button>
      <button type="button" onClick={() => backToLanding()}>Back to landing</button>
    </div>
  )
}

function renderAt(url: string) {
  window.history.replaceState({}, '', url)
  const boot = startScreen(window.location.pathname)
  return render(
    <AppNavProvider initialScreen={boot.screen}>
      <UrlNavigation />
      <Screens />
    </AppNavProvider>,
  )
}

afterEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('cold-load URL correction', () => {
  // A8 -- an unknown route.
  it('an unknown path renders what "/" renders and corrects the URL, preserving search and hash', () => {
    renderAt('/nope?q=1#keep=2')
    expect(screen.getByRole('status')).toHaveTextContent('landing')
    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('?q=1')
    expect(window.location.hash).toBe('#keep=2')
  })

  // A12 -- reload behaviour for the workspace is unchanged.
  it('a cold load of /workspace renders landing and corrects the URL to /', () => {
    renderAt('/workspace')
    expect(screen.getByRole('status')).toHaveTextContent('landing')
    expect(window.location.pathname).toBe('/')
  })

  it('a cold load of /profile renders profile with no URL correction', () => {
    renderAt('/profile')
    expect(screen.getByRole('status')).toHaveTextContent('profile')
    expect(window.location.pathname).toBe('/profile')
  })

  it('the unknown-route correction REPLACES rather than pushes a history entry', () => {
    window.history.replaceState({}, '', '/nope')
    const before = window.history.length
    renderAt('/nope')
    expect(window.location.pathname).toBe('/')
    expect(window.history.length).toBe(before)
  })

  // R7 -- the reserved-key scrub runs on ANY path, not only '/'.
  it('scrubs a reserved fragment key on a routed path, leaving other keys alone', () => {
    renderAt('/profile#token=tok-abc&keep=1')
    expect(screen.getByRole('status')).toHaveTextContent('profile')
    expect(window.location.pathname).toBe('/profile')
    expect(window.location.hash).toBe('#keep=1')
  })

  it('does not write to history at all when nothing needs correcting', () => {
    window.history.replaceState({}, '', '/')
    const before = window.history.length
    renderAt('/')
    expect(window.history.length).toBe(before)
  })
})

// A11 -- Back stays inside the app, proven with a REAL navigation (never a
// `replaceState` + synthetic `PopStateEvent`, which would only prove the
// listener can read a value the test itself just wrote).
describe('Back/Forward', () => {
  it('a real window.history.back() steps back through two real pushes', async () => {
    renderAt('/')
    expect(screen.getByRole('status')).toHaveTextContent('landing')

    await userEvent.click(screen.getByRole('button', { name: 'Enter workspace' }))
    expect(screen.getByRole('status')).toHaveTextContent('workspace')
    expect(window.location.pathname).toBe('/workspace')

    await userEvent.click(screen.getByRole('button', { name: 'Open profile' }))
    expect(screen.getByRole('status')).toHaveTextContent('profile')
    expect(window.location.pathname).toBe('/profile')

    window.history.back()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('workspace'))
    expect(window.location.pathname).toBe('/workspace')

    window.history.back()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('landing'))
    expect(window.location.pathname).toBe('/')
  })
})
