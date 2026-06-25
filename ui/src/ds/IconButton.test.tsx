import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { IconButton } from './IconButton'

// ── Behavior #3: IconButton ───────────────────────────────────────────────────

describe('IconButton — renders as a real <button>', () => {
  it('renders a button element', () => {
    render(<IconButton icon="menu" ariaLabel="Open menu" />)
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument()
  })

  it('renders the icon ligature as a material-symbols-rounded span', () => {
    render(<IconButton icon="send" ariaLabel="Send" />)
    expect(screen.getByText('send')).toHaveClass('material-symbols-rounded')
    // The span is aria-hidden so it doesn't pollute the accessible name
    expect(screen.getByText('send')).toHaveAttribute('aria-hidden', 'true')
  })
})

describe('IconButton — variants', () => {
  const variants = ['standard', 'filled', 'tonal', 'outlined'] as const

  it.each(variants)('applies data-variant="%s"', (variant) => {
    render(<IconButton icon="star" variant={variant} ariaLabel="Favourite" />)
    expect(screen.getByRole('button')).toHaveAttribute('data-variant', variant)
  })

  it('defaults to standard variant', () => {
    render(<IconButton icon="star" ariaLabel="Favourite" />)
    expect(screen.getByRole('button')).toHaveAttribute('data-variant', 'standard')
  })
})

describe('IconButton — accessible name', () => {
  it('applies aria-label from ariaLabel prop', () => {
    render(<IconButton icon="close" ariaLabel="Close dialog" />)
    expect(screen.getByRole('button', { name: 'Close dialog' })).toBeInTheDocument()
  })
})

describe('IconButton — keyboard activation', () => {
  it('fires onClick on Enter key press', async () => {
    const handleClick = vi.fn()
    render(<IconButton icon="casino" ariaLabel="Roll dice" onClick={handleClick} />)
    const btn = screen.getByRole('button', { name: 'Roll dice' })
    btn.focus()
    await userEvent.keyboard('{Enter}')
    expect(handleClick).toHaveBeenCalledTimes(1)
  })

  it('fires onClick on Space key press', async () => {
    const handleClick = vi.fn()
    render(<IconButton icon="casino" ariaLabel="Roll dice" onClick={handleClick} />)
    const btn = screen.getByRole('button', { name: 'Roll dice' })
    btn.focus()
    await userEvent.keyboard(' ')
    expect(handleClick).toHaveBeenCalledTimes(1)
  })

  it('fires onClick on mouse click', async () => {
    const handleClick = vi.fn()
    render(<IconButton icon="add" ariaLabel="Add" onClick={handleClick} />)
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(handleClick).toHaveBeenCalledTimes(1)
  })
})

describe('IconButton — disabled', () => {
  it('is disabled when disabled=true', () => {
    render(<IconButton icon="delete" ariaLabel="Delete" disabled />)
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
  })

  it('does not fire onClick when disabled', async () => {
    const handleClick = vi.fn()
    render(<IconButton icon="delete" ariaLabel="Delete" disabled onClick={handleClick} />)
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(handleClick).not.toHaveBeenCalled()
  })
})

describe('IconButton — selected state', () => {
  it('applies aria-pressed=true when selected=true', () => {
    render(<IconButton icon="favorite" ariaLabel="Like" selected />)
    expect(screen.getByRole('button', { name: 'Like' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('applies aria-pressed=false when selected=false', () => {
    render(<IconButton icon="favorite" ariaLabel="Like" selected={false} />)
    expect(screen.getByRole('button', { name: 'Like' })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('IconButton — size', () => {
  it.each(['small', 'medium', 'large'] as const)('applies data-size="%s"', (size) => {
    render(<IconButton icon="settings" size={size} ariaLabel="Settings" />)
    expect(screen.getByRole('button')).toHaveAttribute('data-size', size)
  })
})
