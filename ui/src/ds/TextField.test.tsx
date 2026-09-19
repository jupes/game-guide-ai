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

// ── ref and ARIA forwarding (record SLASH-12) ────────────────────────────────
// A composer that drives a listbox has to reach the control itself: to focus
// it, to place the caret, and to put aria-activedescendant where a screen
// reader looks for it — on the textarea, not on a wrapper.

describe('TextField — ref forwarding', () => {
  it('gives an object ref the underlying textarea', () => {
    const ref = React.createRef<HTMLInputElement | HTMLTextAreaElement>()
    render(<TextField multiline label="Ask" value="" onChange={vi.fn()} ref={ref} />)
    expect(ref.current).toBe(screen.getByLabelText('Ask'))
    expect(ref.current?.tagName).toBe('TEXTAREA')
  })

  it('gives an object ref the underlying input for a single-line field', () => {
    const ref = React.createRef<HTMLInputElement | HTMLTextAreaElement>()
    render(<TextField label="Name" value="" onChange={vi.fn()} ref={ref} />)
    expect(ref.current?.tagName).toBe('INPUT')
  })

  it('calls a callback ref with the control', () => {
    const seen: (HTMLElement | null)[] = []
    render(<TextField multiline label="Ask" value="" onChange={vi.fn()} ref={(node) => { seen.push(node) }} />)
    expect(seen[0]).toBe(screen.getByLabelText('Ask'))
  })

  it('still grows, so the ref did not displace the internal one', () => {
    const ref = React.createRef<HTMLInputElement | HTMLTextAreaElement>()
    function Harness() {
      const [v, setV] = React.useState('')
      return <TextField multiline autoGrow value={v} onChange={(e) => setV(e.target.value)} label="Ask" ref={ref} />
    }
    render(<Harness />)
    const ta = screen.getByLabelText('Ask') as HTMLTextAreaElement
    stubScrollHeight(ta, 96)
    fireEvent.change(ta, { target: { value: 'several\nlines' } })
    expect(ta.style.height).toBe('96px')
    expect(ref.current).toBe(ta)
  })
})

describe('TextField — ARIA and focus forwarding', () => {
  it('puts the listbox wiring on the control itself, where a screen reader reads it', () => {
    render(
      <TextField
        multiline
        value=""
        onChange={vi.fn()}
        aria-label="Message"
        aria-autocomplete="list"
        aria-controls="tools-listbox"
        aria-activedescendant="tools-option-2"
        aria-describedby="brief-hint"
        aria-invalid
      />,
    )
    const control = screen.getByRole('textbox', { name: 'Message' })
    expect(control).toHaveAttribute('aria-autocomplete', 'list')
    expect(control).toHaveAttribute('aria-controls', 'tools-listbox')
    expect(control).toHaveAttribute('aria-activedescendant', 'tools-option-2')
    expect(control).toHaveAttribute('aria-describedby', 'brief-hint')
    expect(control).toHaveAttribute('aria-invalid', 'true')
  })

  it('forwards the same ARIA on a single-line field', () => {
    render(<TextField value="" onChange={vi.fn()} aria-label="Search" aria-controls="results" />)
    expect(screen.getByRole('textbox', { name: 'Search' })).toHaveAttribute('aria-controls', 'results')
  })

  it('reports focus and blur without losing its own focus styling', async () => {
    const onFocus = vi.fn()
    const onBlur = vi.fn()
    render(<TextField multiline label="Ask" value="" onChange={vi.fn()} onFocus={onFocus} onBlur={onBlur} />)
    const control = screen.getByLabelText('Ask')
    await userEvent.click(control)
    expect(onFocus).toHaveBeenCalledTimes(1)
    expect(control.parentElement).toHaveClass('aether-field__row--focus')
    await userEvent.tab()
    expect(onBlur).toHaveBeenCalledTimes(1)
    expect(control.parentElement).not.toHaveClass('aether-field__row--focus')
  })

  it('adds no ARIA attribute that was not asked for', () => {
    render(<TextField multiline label="Ask" value="" onChange={vi.fn()} />)
    const control = screen.getByLabelText('Ask')
    for (const attribute of ['aria-controls', 'aria-activedescendant', 'aria-autocomplete', 'aria-invalid', 'aria-label']) {
      expect(control).not.toHaveAttribute(attribute)
    }
  })
})
