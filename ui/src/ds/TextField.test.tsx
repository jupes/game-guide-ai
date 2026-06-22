import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TextField } from './TextField'

// ── Behavior #4: TextField ────────────────────────────────────────────────────

describe('TextField — controlled value/onChange', () => {
  it('displays the value prop', () => {
    render(<TextField value="hello" onChange={vi.fn()} label="Name" />)
    expect(screen.getByDisplayValue('hello')).toBeInTheDocument()
  })

  it('fires onChange when the user types', async () => {
    const handleChange = vi.fn()
    render(<TextField value="" onChange={handleChange} label="Name" />)
    const input = screen.getByRole('textbox')
    await userEvent.type(input, 'a')
    expect(handleChange).toHaveBeenCalled()
  })
})

describe('TextField — error + supportingText', () => {
  it('renders supportingText below the field', () => {
    render(<TextField label="HP" supportingText="Must be a positive number" />)
    expect(screen.getByText('Must be a positive number')).toBeInTheDocument()
  })

  it('applies data-error="true" when error=true', () => {
    render(<TextField label="HP" error supportingText="Error!" />)
    // The root wrapper should expose error state
    const wrapper = screen.getByTestId('textfield-root')
    expect(wrapper).toHaveAttribute('data-error', 'true')
  })

  it('renders error supportingText visible', () => {
    render(<TextField label="HP" error supportingText="HP must be positive" />)
    expect(screen.getByText('HP must be positive')).toBeInTheDocument()
  })
})

describe('TextField — leading/trailing icons', () => {
  it('renders a leading icon span', () => {
    render(<TextField label="Search" leadingIcon="search" />)
    expect(screen.getByText('search')).toHaveClass('material-symbols-rounded')
  })

  it('renders a trailing icon span', () => {
    render(<TextField label="Password" trailingIcon="visibility" />)
    expect(screen.getByText('visibility')).toHaveClass('material-symbols-rounded')
  })

  it('renders both icons when both props are provided', () => {
    render(<TextField label="Name" leadingIcon="badge" trailingIcon="clear" />)
    expect(screen.getByText('badge')).toBeInTheDocument()
    expect(screen.getByText('clear')).toBeInTheDocument()
  })
})

describe('TextField — multiline', () => {
  it('renders a textarea when multiline=true', () => {
    render(<TextField label="Notes" multiline rows={4} />)
    expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA')
  })

  it('renders an input when multiline=false (default)', () => {
    render(<TextField label="Name" />)
    expect(screen.getByRole('textbox').tagName).toBe('INPUT')
  })
})

describe('TextField — onKeyDown extension (Enter-to-send)', () => {
  it('fires onKeyDown with the keyboard event when a key is pressed', async () => {
    const handleKeyDown = vi.fn()
    render(<TextField label="Message" value="" onChange={vi.fn()} onKeyDown={handleKeyDown} />)
    const input = screen.getByRole('textbox')
    await userEvent.type(input, '{Enter}')
    expect(handleKeyDown).toHaveBeenCalled()
    const event = handleKeyDown.mock.calls[0][0] as React.KeyboardEvent
    expect(event.key).toBe('Enter')
  })

  it('fires onKeyDown when Enter is pressed on a multiline field', async () => {
    const handleKeyDown = vi.fn()
    render(
      <TextField
        label="Message"
        value=""
        onChange={vi.fn()}
        onKeyDown={handleKeyDown}
        multiline
      />
    )
    const textarea = screen.getByRole('textbox')
    await userEvent.type(textarea, '{Enter}')
    expect(handleKeyDown).toHaveBeenCalled()
  })
})

describe('TextField — label', () => {
  it('renders the label text', () => {
    render(<TextField label="Character Name" />)
    expect(screen.getByText('Character Name')).toBeInTheDocument()
  })
})

describe('TextField — disabled', () => {
  it('disables the input when disabled=true', () => {
    render(<TextField label="Locked" disabled />)
    expect(screen.getByRole('textbox')).toBeDisabled()
  })
})

describe('TextField — variant', () => {
  it('applies data-variant="outlined" by default', () => {
    render(<TextField label="Name" />)
    expect(screen.getByTestId('textfield-root')).toHaveAttribute('data-variant', 'outlined')
  })

  it('applies data-variant="filled" when variant=filled', () => {
    render(<TextField label="Name" variant="filled" />)
    expect(screen.getByTestId('textfield-root')).toHaveAttribute('data-variant', 'filled')
  })
})
