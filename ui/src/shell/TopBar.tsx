/**
 * TopBar — Brand header with a dark/light theme toggle Switch.
 */

import * as React from 'react'
import { Switch } from '../ds/Switch'
import { useTheme } from '../ds/theme'
import './TopBar.css'

export function TopBar(): React.JSX.Element {
  const { theme, setTheme } = useTheme()

  function handleThemeChange(next: boolean): void {
    setTheme(next ? 'dark' : 'light')
  }

  return (
    <header className="top-bar">
      <div className="top-bar__brand">
        <span
          className="material-symbols-rounded top-bar__brand-icon"
          aria-hidden="true"
        >
          auto_stories
        </span>
        <span className="top-bar__brand-name">Aetheril</span>
      </div>

      <Switch
        checked={theme === 'dark'}
        onChange={handleThemeChange}
        ariaLabel="Dark theme"
      />
    </header>
  )
}
