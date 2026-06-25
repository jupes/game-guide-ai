import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from './Button'

// ── Behavior #2: Button ───────────────────────────────────────────────────────

describe('Button — variants', () => {
  const variants = ['filled', 'tonal', 'elevated', 'outlined', 'text'] as const

  it.each(variants)('renders variant="%s" as a <button> element', (variant) => {
    render(<Button variant={variant}>{variant}</Button>)
    const btn = screen.getByRole('button', { name: variant })
    expect(btn).toBeInTheDocument()
  })

  it.each(variants)('applies data-variant="%s" attribute', (variant) => {
    render(<Button variant={variant}>{variant}</Button>)
    const btn = screen.getByRole('button')
    expect(btn).toHaveAttribute('data-variant', variant)
  })
})

describe('Button — onClick', () => {
  it('fires onClick when clicked', async () => {
    const handleClick = vi.fn()
    render(<Button onClick={handleClick}>Click me</Button>)
    await userEvent.click(screen.getByRole('button', { name: 'Click me' }))
    expect(handleClick).toHaveBeenCalledTimes(1)
  })

  it('does NOT fire onClick when disabled', async () => {
    const handleClick = vi.fn()
    render(<Button disabled onClick={handleClick}>Disabled</Button>)
    await userEvent.click(screen.getByRole('button', { name: 'Disabled' }))
    expect(handleClick).not.toHaveBeenCalled()
  })
})

describe('Button — icon slots', () => {
  it('renders a leading icon span with the symbol name as text', () => {
    render(<Button icon="casino">Roll</Button>)
    expect(screen.getByText('casino')).toBeInTheDocument()
    expect(screen.getByText('casino')).toHaveClass('material-symbols-rounded')
  })

  it('renders a trailing icon span when trailingIcon is set', () => {
    render(<Button trailingIcon="chevron_right">Next</Button>)
    expect(screen.getByText('chevron_right')).toBeInTheDocument()
    expect(screen.getByText('chevron_right')).toHaveClass('material-symbols-rounded')
  })

  it('renders both leading and trailing icons together', () => {
    render(<Button icon="star" trailingIcon="arrow_forward">Go</Button>)
    expect(screen.getByText('star')).toBeInTheDocument()
    expect(screen.getByText('arrow_forward')).toBeInTheDocument()
  })

  it('renders no icon spans when neither icon prop is provided', () => {
    render(<Button>Plain</Button>)
    const btn = screen.getByRole('button')
    const iconSpans = btn.querySelectorAll('.material-symbols-rounded')
    expect(iconSpans).toHaveLength(0)
  })
})

describe('Button — size', () => {
  it('applies data-size="small" when size=small', () => {
    render(<Button size="small">Small</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('data-size', 'small')
  })

  it('applies data-size="medium" by default', () => {
    render(<Button>Medium</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('data-size', 'medium')
  })

  it('applies data-size="large" when size=large', () => {
    render(<Button size="large">Large</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('data-size', 'large')
  })
})

describe('Button — disabled', () => {
  it('has the disabled attribute when disabled=true', () => {
    render(<Button disabled>Disabled</Button>)
    expect(screen.getByRole('button', { name: 'Disabled' })).toBeDisabled()
  })
})

describe('Button — fullWidth', () => {
  it('applies data-full-width="true" when fullWidth=true', () => {
    render(<Button fullWidth>Full</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('data-full-width', 'true')
  })
})

describe('Button — type', () => {
  it('defaults to type="button"', () => {
    render(<Button>Default</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('type', 'button')
  })

  it('forwards type="submit"', () => {
    render(<Button type="submit">Submit</Button>)
    expect(screen.getByRole('button')).toHaveAttribute('type', 'submit')
  })
})

describe('Button — children', () => {
  it('renders children text', () => {
    render(<Button>Hello World</Button>)
    expect(screen.getByRole('button', { name: 'Hello World' })).toBeInTheDocument()
  })
})
