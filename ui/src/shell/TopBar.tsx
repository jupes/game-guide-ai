/**
 * TopBar — Brand header with a dark/light theme toggle Switch.
 */

import * as React from 'react'
import { Switch } from '../ds/Switch'
import { useTheme } from '../ds/theme'

export function TopBar(): React.JSX.Element {
  const { theme, setTheme } = useTheme()

  function handleThemeChange(next: boolean): void {
    setTheme(next ? 'dark' : 'light')
  }

  return (
    <header
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 16px',
        height: 64,
        borderBottom: '1px solid var(--aether-outline-variant)',
        background: 'var(--aether-surface)',
        flexShrink: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span
          className="material-symbols-rounded"
          aria-hidden="true"
          style={{ fontSize: 24, color: 'var(--aether-primary)' }}
        >
          auto_stories
        </span>
        <span style={{ fontWeight: 700, fontSize: 18, color: 'var(--aether-on-surface)' }}>
          Aetheril
        </span>
      </div>

      <Switch
        checked={theme === 'dark'}
        onChange={handleThemeChange}
        ariaLabel="Dark theme"
      />
    </header>
  )
}
