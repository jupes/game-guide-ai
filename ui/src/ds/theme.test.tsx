import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, useTheme } from './theme'

// ── localStorage stub ─────────────────────────────────────────────────────────
// jsdom 29 ships its own localStorage that may not have .clear() exposed in
// every vitest runner configuration. Use a simple in-memory stub so the tests
// are hermetic regardless of the host environment.

function makeLocalStorageStub() {
  let store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => { store[key] = value },
    removeItem: (key: string) => { delete store[key] },
    clear: () => { store = {} },
    get length() { return Object.keys(store).length },
    key: (index: number) => Object.keys(store)[index] ?? null,
  }
}

// ── helpers ───────────────────────────────────────────────────────────────────

/** A minimal consumer component that exposes the hook's API into the DOM. */
function ThemeConsumer() {
  const { theme, setTheme, toggleTheme } = useTheme()
  return (
    <div>
      <span data-testid="current-theme">{theme}</span>
      <button type="button" onClick={() => setTheme('dark')}>set-dark</button>
      <button type="button" onClick={() => setTheme('light')}>set-light</button>
      <button type="button" onClick={toggleTheme}>toggle</button>
    </div>
  )
}

/** Render the provider + consumer and return Testing Library utils. */
function renderTheme(initialTheme?: 'light' | 'dark') {
  return render(
    <ThemeProvider initialTheme={initialTheme}>
      <ThemeConsumer />
    </ThemeProvider>,
  )
}

// ── setup / teardown ──────────────────────────────────────────────────────────

let lsMock: ReturnType<typeof makeLocalStorageStub>

beforeEach(() => {
  lsMock = makeLocalStorageStub()
  vi.stubGlobal('localStorage', lsMock)
  document.documentElement.removeAttribute('data-theme')
})

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
  vi.unstubAllGlobals()
})

// ── Behavior #1: defaults to light Parchment ─────────────────────────────────

describe('ThemeProvider — light Parchment default', () => {
  it('reports "light" as the current theme when no stored preference exists', () => {
    renderTheme()
    expect(screen.getByTestId('current-theme').textContent).toBe('light')
  })

  it('does NOT set data-theme="dark" on <html> by default', () => {
    renderTheme()
    // Light is the :root default in the DS; we accept either no attribute or "light".
    const attr = document.documentElement.getAttribute('data-theme')
    expect(attr === null || attr === 'light').toBe(true)
  })
})

// ── Behavior #1: toggles to dark Tavern ──────────────────────────────────────

describe('ThemeProvider — toggle to dark Tavern', () => {
  it('sets data-theme="dark" on document.documentElement when theme is set to dark', async () => {
    renderTheme()
    await userEvent.click(screen.getByText('set-dark'))
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(screen.getByTestId('current-theme').textContent).toBe('dark')
  })

  it('removes data-theme="dark" (or sets "light") when toggled back to light', async () => {
    renderTheme()
    await userEvent.click(screen.getByText('set-dark'))
    await userEvent.click(screen.getByText('set-light'))
    const attr = document.documentElement.getAttribute('data-theme')
    expect(attr === null || attr === 'light').toBe(true)
    expect(screen.getByTestId('current-theme').textContent).toBe('light')
  })

  it('toggleTheme flips from light → dark', async () => {
    renderTheme()
    await userEvent.click(screen.getByText('toggle'))
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(screen.getByTestId('current-theme').textContent).toBe('dark')
  })

  it('toggleTheme flips from dark → light', async () => {
    renderTheme()
    await userEvent.click(screen.getByText('set-dark'))
    await userEvent.click(screen.getByText('toggle'))
    const attr = document.documentElement.getAttribute('data-theme')
    expect(attr === null || attr === 'light').toBe(true)
    expect(screen.getByTestId('current-theme').textContent).toBe('light')
  })
})

// ── Behavior #1: persists the choice across reload ───────────────────────────

describe('ThemeProvider — persistence across reload', () => {
  it('writes the chosen theme to localStorage', async () => {
    renderTheme()
    await userEvent.click(screen.getByText('set-dark'))
    expect(lsMock.getItem('aetheril-theme')).toBe('dark')
  })

  it('reads the stored dark preference on a fresh mount', () => {
    // Simulate a previous session that saved "dark"
    lsMock.setItem('aetheril-theme', 'dark')
    renderTheme()
    expect(screen.getByTestId('current-theme').textContent).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('reads the stored light preference and stays in light mode', () => {
    lsMock.setItem('aetheril-theme', 'light')
    renderTheme()
    const attr = document.documentElement.getAttribute('data-theme')
    expect(attr === null || attr === 'light').toBe(true)
    expect(screen.getByTestId('current-theme').textContent).toBe('light')
  })

  it('persists back to light after toggling from dark', async () => {
    lsMock.setItem('aetheril-theme', 'dark')
    renderTheme()
    await userEvent.click(screen.getByText('toggle'))
    expect(lsMock.getItem('aetheril-theme')).toBe('light')
  })

  it('ignores a corrupted localStorage value and defaults to light', () => {
    lsMock.setItem('aetheril-theme', 'system') // invalid value
    renderTheme()
    expect(screen.getByTestId('current-theme').textContent).toBe('light')
  })
})

// ── useTheme throws when used outside the provider ───────────────────────────

describe('useTheme — guard', () => {
  it('throws when used outside <ThemeProvider>', () => {
    // Suppress the React error boundary console noise in the test output
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<ThemeConsumer />)).toThrow()
    consoleError.mockRestore()
  })
})
