/**
 * TopBar — Brand header. The dark/light theme toggle moved to the UserMenu
 * (swe1.11); channel switching lives in the AppHeader (swe1.4).
 */

import * as React from 'react'
import './TopBar.css'

export function TopBar(): React.JSX.Element {
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
    </header>
  )
}
