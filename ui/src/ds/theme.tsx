/**
 * ThemeProvider + useTheme
 *
 * Behavior #1 (Aetheril design system):
 *   - Light Parchment is the default (no data-theme attribute, or data-theme="light").
 *   - Dark Tavern sets data-theme="dark" on document.documentElement.
 *   - Choice is persisted to localStorage under key "aetheril-theme".
 *   - On fresh mount the stored preference is read back.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

// ── Types ────────────────────────────────────────────────────────────────────

export type Theme = 'light' | 'dark'

interface ThemeContextValue {
  /** The active theme name. */
  theme: Theme
  /** Set an explicit theme. */
  setTheme: (next: Theme) => void
  /** Flip between light and dark. */
  toggleTheme: () => void
}

// ── Context ──────────────────────────────────────────────────────────────────

const ThemeContext = createContext<ThemeContextValue | null>(null)

// ── Helpers ──────────────────────────────────────────────────────────────────

const STORAGE_KEY = 'aetheril-theme'

function isValidTheme(value: string | null): value is Theme {
  return value === 'light' || value === 'dark'
}

/** Apply the theme to the document root and persist it. */
function applyTheme(next: Theme): void {
  if (next === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark')
  } else {
    // Light is the :root default — remove the attribute (or set to "light").
    // Removing is idiomatic: the design system's :root is already light.
    document.documentElement.removeAttribute('data-theme')
  }
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // localStorage may be unavailable in some environments; ignore silently.
  }
}

/** Read the stored preference. Returns 'light' for any invalid/missing value. */
function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return isValidTheme(stored) ? stored : 'light'
  } catch {
    return 'light'
  }
}

// ── Provider ─────────────────────────────────────────────────────────────────

interface ThemeProviderProps {
  children: ReactNode
  /**
   * Override the initial theme (useful in tests or SSR).
   * When omitted the stored localStorage value is used, defaulting to 'light'.
   */
  initialTheme?: Theme
}

export function ThemeProvider({ children, initialTheme }: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = readStoredTheme()
    const resolved = initialTheme ?? stored
    // Apply synchronously on first render so there is no flash between
    // the JS module loading and the first React commit.
    applyTheme(resolved)
    return resolved
  })

  const setTheme = useCallback((next: Theme) => {
    applyTheme(next)
    setThemeState(next)
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((current) => {
      const next: Theme = current === 'light' ? 'dark' : 'light'
      applyTheme(next)
      return next
    })
  }, [])

  // Keep the DOM in sync if theme state is changed externally (e.g. HMR).
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, setTheme, toggleTheme }),
    [theme, setTheme, toggleTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

// ── Hook ─────────────────────────────────────────────────────────────────────

/**
 * Access the active theme and controls.
 * Must be used inside a <ThemeProvider>; throws otherwise.
 */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider; HMR-only rule, not a correctness issue
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (ctx === null) {
    throw new Error('useTheme must be used within a <ThemeProvider>.')
  }
  return ctx
}
