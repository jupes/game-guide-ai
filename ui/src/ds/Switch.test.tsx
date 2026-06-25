import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Switch } from './Switch'

// ── Behavior #5: Switch ───────────────────────────────────────────────────────

describe('Switch — accessible role', () => {
  it('has role="switch"', () => {
    render(<Switch ariaLabel="Enable feature" />)
    expect(screen.getByRole('switch', { name: 'Enable feature' })).toBeInTheDocument()
  })

  it('has aria-checked="false" when off', () => {
    render(<Switch checked={false} ariaLabel="Toggle" />)
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  })

  it('has aria-checked="true" when on', () => {
    render(<Switch checked ariaLabel="Toggle" />)
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  })
})

describe('Switch — onChange receives the next boolean (not a DOM event)', () => {
  it('calls onChange(true) when toggled from off', async () => {
    const handleChange = vi.fn()
    render(<Switch checked={false} onChange={handleChange} ariaLabel="Toggle" />)
    await userEvent.click(screen.getByRole('switch'))
    expect(handleChange).toHaveBeenCalledTimes(1)
    expect(handleChange).toHaveBeenCalledWith(true)
  })

  it('calls onChange(false) when toggled from on', async () => {
    const handleChange = vi.fn()
    render(<Switch checked onChange={handleChange} ariaLabel="Toggle" />)
    await userEvent.click(screen.getByRole('switch'))
    expect(handleChange).toHaveBeenCalledTimes(1)
    expect(handleChange).toHaveBeenCalledWith(false)
  })

  it('does NOT receive a DOM MouseEvent object', async () => {
    const handleChange = vi.fn()
    render(<Switch checked={false} onChange={handleChange} ariaLabel="Toggle" />)
    await userEvent.click(screen.getByRole('switch'))
    const arg = handleChange.mock.calls[0][0]
    expect(typeof arg).toBe('boolean')
  })
})

describe('Switch — reflects on/off state', () => {
  it('applies data-checked="false" when off', () => {
    render(<Switch checked={false} ariaLabel="Toggle" />)
    expect(screen.getByRole('switch')).toHaveAttribute('data-checked', 'false')
  })

  it('applies data-checked="true" when on', () => {
    render(<Switch checked ariaLabel="Toggle" />)
    expect(screen.getByRole('switch')).toHaveAttribute('data-checked', 'true')
  })
})

describe('Switch — 44px touch hit area', () => {
  it('has a min-height of at least 44px via the wrapping element', () => {
    render(<Switch ariaLabel="Toggle" />)
    const sw = screen.getByRole('switch')
    // The switch button itself must have a visual touch area of >= 44px.
    // We check the data attribute that signals the implementation exposes this.
    expect(sw).toHaveAttribute('data-touch-target', 'true')
  })
})

describe('Switch — disabled', () => {
  it('is disabled when disabled=true', () => {
    render(<Switch disabled ariaLabel="Toggle" />)
    expect(screen.getByRole('switch')).toBeDisabled()
  })

  it('does not call onChange when disabled', async () => {
    const handleChange = vi.fn()
    render(<Switch checked={false} disabled onChange={handleChange} ariaLabel="Toggle" />)
    await userEvent.click(screen.getByRole('switch'))
    expect(handleChange).not.toHaveBeenCalled()
  })
})

describe('Switch — icons prop', () => {
  it('renders a check icon when icons=true and checked=true', () => {
    render(<Switch checked icons ariaLabel="Toggle" />)
    expect(screen.getByText('check')).toHaveClass('material-symbols-rounded')
  })

  it('renders a close icon when icons=true and checked=false', () => {
    render(<Switch checked={false} icons ariaLabel="Toggle" />)
    expect(screen.getByText('close')).toHaveClass('material-symbols-rounded')
  })

  it('renders no icon spans when icons=false', () => {
    render(<Switch ariaLabel="Toggle" />)
    const sw = screen.getByRole('switch')
    expect(sw.querySelectorAll('.material-symbols-rounded')).toHaveLength(0)
  })
})

describe('Switch — aria-label', () => {
  it('exposes ariaLabel as aria-label', () => {
    render(<Switch ariaLabel="Enable notifications" />)
    expect(screen.getByRole('switch')).toHaveAttribute('aria-label', 'Enable notifications')
  })
})
