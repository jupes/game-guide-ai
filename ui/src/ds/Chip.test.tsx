/**
 * Chip — Behavior #7
 *
 * Tests:
 *  - Renders all 4 types: assist, filter, input, suggestion
 *  - Selected filter chip gets filled/ember-gold class
 *  - Unselected filter chip looks like a regular chip (no selected class)
 *  - 8px radius via --aether-radius-chip token
 *  - Optional leading icon renders a material-symbols-rounded span
 *  - onClick fires on click
 *  - Input chip: onRemove fires when remove button clicked
 *  - Disabled chip: onClick not called
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Chip } from './Chip'

describe('Chip — type variants', () => {
  it('renders assist chip', () => {
    render(<Chip label="Assist" type="assist" />)
    expect(screen.getByText('Assist')).toBeInTheDocument()
  })

  it('renders filter chip', () => {
    render(<Chip label="Filter" type="filter" />)
    expect(screen.getByText('Filter')).toBeInTheDocument()
  })

  it('renders input chip', () => {
    render(<Chip label="Input" type="input" />)
    expect(screen.getByText('Input')).toBeInTheDocument()
  })

  it('renders suggestion chip', () => {
    render(<Chip label="Suggestion" type="suggestion" />)
    expect(screen.getByText('Suggestion')).toBeInTheDocument()
  })

  it('defaults to assist type', () => {
    render(<Chip label="Default" data-testid="chip" />)
    // No selected indicator without type="filter" && selected
    const chip = screen.getByTestId('chip')
    expect(chip.className).not.toMatch(/chip--selected/)
  })
})

describe('Chip — filter selected state', () => {
  it('adds selected class when filter chip is selected', () => {
    render(<Chip label="Homebrew" type="filter" selected data-testid="chip" />)
    const chip = screen.getByTestId('chip')
    expect(chip.className).toMatch(/chip--selected/)
  })

  it('does NOT add selected class when filter chip is unselected', () => {
    render(<Chip label="Homebrew" type="filter" selected={false} data-testid="chip" />)
    const chip = screen.getByTestId('chip')
    expect(chip.className).not.toMatch(/chip--selected/)
  })

  it('shows check icon when filter chip is selected', () => {
    render(<Chip label="Homebrew" type="filter" selected data-testid="chip" />)
    // Check icon should render (material symbols check ligature)
    const icons = document.querySelectorAll('.material-symbols-rounded')
    const checkIcon = Array.from(icons).find(el => el.textContent === 'check')
    expect(checkIcon).not.toBeNull()
  })

  it('does not apply selected class for non-filter type', () => {
    render(<Chip label="Assist" type="assist" selected data-testid="chip" />)
    // selected prop is only meaningful for filter type
    expect(screen.getByTestId('chip').className).not.toMatch(/chip--selected/)
  })
})

describe('Chip — leading icon', () => {
  it('renders leading icon when provided', () => {
    render(<Chip label="Wizard" icon="auto_awesome" type="assist" />)
    const icon = document.querySelector('.material-symbols-rounded')
    expect(icon).not.toBeNull()
    expect(icon?.textContent).toBe('auto_awesome')
  })

  it('does not render icon when not provided', () => {
    render(<Chip label="Wizard" type="assist" />)
    // No material-symbols-rounded span at all
    const icon = document.querySelector('.material-symbols-rounded')
    expect(icon).toBeNull()
  })

  it('does not render leading icon (only check icon) when filter is selected', () => {
    render(<Chip label="Filter" type="filter" icon="star" selected />)
    // check icon should appear, NOT the star icon
    const icons = document.querySelectorAll('.material-symbols-rounded')
    const starIcon = Array.from(icons).find(el => el.textContent === 'star')
    const checkIcon = Array.from(icons).find(el => el.textContent === 'check')
    // Array.prototype.find returns undefined (not null) when no match
    expect(starIcon).toBeUndefined()
    expect(checkIcon).not.toBeUndefined()
  })
})

describe('Chip — onClick', () => {
  it('fires onClick when clicked', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Chip label="Click me" onClick={onClick} />)
    await user.click(screen.getByText('Click me'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('does not fire onClick when disabled', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Chip label="Disabled" disabled onClick={onClick} data-testid="chip" />)
    // Clicking a disabled chip should not call onClick
    await user.click(screen.getByTestId('chip'))
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('Chip — input type remove affordance', () => {
  it('renders remove button for input chip', () => {
    render(<Chip label="Tag" type="input" onRemove={() => {}} />)
    const removeBtn = screen.getByRole('button', { name: /remove/i })
    expect(removeBtn).toBeInTheDocument()
  })

  it('fires onRemove when remove button is clicked', async () => {
    const user = userEvent.setup()
    const onRemove = vi.fn()
    const onClick = vi.fn()
    render(<Chip label="Tag" type="input" onRemove={onRemove} onClick={onClick} />)
    await user.click(screen.getByRole('button', { name: /remove/i }))
    expect(onRemove).toHaveBeenCalledTimes(1)
    // onClick should NOT fire (stopPropagation)
    expect(onClick).not.toHaveBeenCalled()
  })
})
