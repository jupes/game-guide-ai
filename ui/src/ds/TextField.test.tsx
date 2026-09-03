import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import * as React from 'react'
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

// ── autoGrow (pp6q.1.4) ───────────────────────────────────────────────────────
// A DS extension, like onKeyDown above: not in aetheril-design-system's
// TextField.d.ts. jsdom performs no layout, so scrollHeight is stubbed — an
// unstubbed run would silently measure 0 and prove nothing.

function stubScrollHeight(el: HTMLElement, px: number) {
  Object.defineProperty(el, 'scrollHeight', { value: px, configurable: true })
}

describe('TextField — autoGrow', () => {
  it('does NOT set an inline height when autoGrow is off (the default)', async () => {
    // Guard for every other TextField in the app: opting in must be the only
    // way to change behavior.
    render(<TextField multiline rows={2} value="" onChange={vi.fn()} label="Ask" />)
    const ta = screen.getByLabelText('Ask') as HTMLTextAreaElement
    stubScrollHeight(ta, 500)
    await userEvent.type(ta, 'x')
    expect(ta.style.height).toBe('')
    expect(ta.getAttribute('rows')).toBe('2')
  })

  it('grows to fit its content when autoGrow is on', () => {
    function Harness() {
      const [v, setV] = React.useState('')
      return (
        <TextField multiline autoGrow value={v}
          onChange={(e) => setV(e.target.value)} label="Ask" />
      )
    }
    const { rerender } = render(<Harness />)
    const ta = screen.getByLabelText('Ask') as HTMLTextAreaElement
    stubScrollHeight(ta, 96)
    rerender(<Harness />)
    fireEvent.change(ta, { target: { value: 'several\nlines\nof\ntext' } })
    expect(ta.style.height).toBe('96px')
  })

  it('caps growth and scrolls internally past the maximum', () => {
    function Harness() {
      const [v, setV] = React.useState('')
      return (
        <TextField multiline autoGrow autoGrowMaxPx={160} value={v}
          onChange={(e) => setV(e.target.value)} label="Ask" />
      )
    }
    render(<Harness />)
    const ta = screen.getByLabelText('Ask') as HTMLTextAreaElement
    stubScrollHeight(ta, 900)
    fireEvent.change(ta, { target: { value: 'a very long draft' } })
    expect(ta.style.height).toBe('160px')
    expect(ta.style.overflowY).toBe('auto')
  })
})
