/**
 * NavRail.test.tsx -- the 56 px navigation rail (agent-forge-harness-1kg.6.3, T-11;
 * LAYOUT-7, I-3, C-9). jsdom owns semantics: the landmark, names, state attributes
 * and which controls exist. The rail's 56 px box is proven in Chromium by the shell
 * stories.
 */

import * as React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NavRail, type NavRailProps } from './NavRail'

function Rail(props: Partial<NavRailProps>): React.JSX.Element {
  const ref = React.useRef<HTMLButtonElement>(null)
  return <NavRail controls="drawer-host" expanded={false} onOpen={() => {}} openButtonRef={ref} {...props} />
}

describe('NavRail', () => {
  it('is a landmark named Navigation rail, not Workbench rail (C-9)', () => {
    render(<Rail />)
    expect(screen.getByRole('navigation', { name: 'Navigation rail' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: /workbench/i })).toBeNull()
  })

  it('Open navigation reports its state and names the drawer it opens', () => {
    const { rerender } = render(<Rail expanded={false} />)
    const open = screen.getByRole('button', { name: 'Open navigation' })
    expect(open).toHaveAttribute('aria-expanded', 'false')
    expect(open).toHaveAttribute('aria-controls', 'drawer-host')
    rerender(<Rail expanded />)
    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('Open navigation opens, and is the button the shell focuses when the drawer closes', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    const ref = React.createRef<HTMLButtonElement>()
    render(<NavRail controls="drawer-host" expanded={false} onOpen={onOpen} openButtonRef={ref} />)
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(ref.current).toBe(screen.getByRole('button', { name: 'Open navigation' }))
  })

  it('has no Campaign Library button unless the Workbench is active (X-9)', () => {
    const { rerender } = render(<Rail />)
    expect(screen.queryByRole('button', { name: 'Campaign Library' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Campaign documents' })).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)
    rerender(<Rail onToggleLibrary={() => {}} />)
    expect(screen.getByRole('button', { name: 'Campaign Library' })).toBeInTheDocument()
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })

  it('Campaign Library names the panel it toggles, says whether it is open, and has a tooltip equal to its name (LIB-8)', () => {
    const { rerender } = render(<Rail onToggleLibrary={() => {}} libraryControls="library-panel" libraryExpanded={false} />)
    const button = screen.getByRole('button', { name: 'Campaign Library' })
    expect(button).toHaveAttribute('aria-controls', 'library-panel')
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).toHaveAttribute('title', 'Campaign Library')
    rerender(<Rail onToggleLibrary={() => {}} libraryControls="library-panel" libraryExpanded />)
    expect(screen.getByRole('button', { name: 'Campaign Library' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('Campaign Library calls its handler with the button, which is where focus returns', async () => {
    const user = userEvent.setup()
    const onToggleLibrary = vi.fn()
    const ref = React.createRef<HTMLButtonElement>()
    render(<Rail onToggleLibrary={onToggleLibrary} libraryButtonRef={ref} />)
    const button = screen.getByRole('button', { name: 'Campaign Library' })
    await user.click(button)
    expect(onToggleLibrary).toHaveBeenCalledTimes(1)
    expect(onToggleLibrary).toHaveBeenCalledWith(button)
    expect(ref.current).toBe(button)
  })

  it('hides its icon ligatures from assistive technology', () => {
    const { container } = render(<Rail onToggleLibrary={() => {}} />)
    const icons = container.querySelectorAll('.material-symbols-rounded')
    expect(icons.length).toBe(2)
    for (const icon of icons) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it('is operable from the keyboard', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    render(<Rail onOpen={onOpen} />)
    await user.tab()
    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledTimes(1)
  })
})
