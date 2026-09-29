/**
 * NavSettings — the narrow layout's settings block (agent-forge-harness-0rn).
 *
 * Below 768px AppHeader keeps only the channels; the model picker and the
 * theme control move here, into the LeftNav drawer, above the account
 * footer. They are settings, not per-message actions, and a phone's chrome
 * height is the chat's budget.
 *
 * WorkspaceShell mounts this at the narrow layout EVEN WHILE THE DRAWER IS
 * CLOSED: ModelPicker is what loads the /models catalog that ChatPane sends
 * by, so exactly one picker is mounted at every layout (F-5).
 */

import * as React from 'react'
import { Switch } from '../ds/Switch'
import { useTheme } from '../ds/theme'
import { ModelPicker } from './ModelPicker'
import './NavSettings.css'

const SETTINGS = 'Settings'
const DARK_THEME = 'Dark theme'

export function NavSettings(): React.JSX.Element {
  const { theme, toggleTheme } = useTheme()

  return (
    <div role="group" aria-label={SETTINGS} className="nav-settings">
      <ModelPicker />
      <div className="nav-settings__theme">
        <span className="nav-settings__theme-label">{DARK_THEME}</span>
        <Switch checked={theme === 'dark'} onChange={toggleTheme} ariaLabel={DARK_THEME} />
      </div>
    </div>
  )
}
