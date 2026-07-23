/**
 * Landing — Aetheril-branded entry screen.
 *
 * Shows the brand, tagline, a primary CTA, and optional mode entry chips.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { Card } from '../ds/Card'
import { Chip } from '../ds/Chip'
import { useAppNav } from './AppNav'
import { useCurrentUser } from './currentUser'
import { modesForRole, accentClass } from './modes'
import './Landing.css'
import './modeAccents.css'

export function Landing(): React.JSX.Element {
  const { enterWorkspace } = useAppNav()
  const { user } = useCurrentUser()

  return (
    <div className="landing">
      <Card className="landing__card">
        {/* Brand */}
        <div className="landing__brand">
          <span
            className="material-symbols-rounded landing__brand-icon"
            aria-hidden="true"
          >
            auto_stories
          </span>
        </div>

        <h1 className="landing__title">Aetheril</h1>

        <p className="landing__tagline">Grounded answers from the rulebooks</p>

        {/* Primary CTA */}
        <div className="landing__cta">
          <Button
            variant="filled"
            size="large"
            icon="login"
            onClick={() => enterWorkspace()}
            className="landing__cta-button"
          >
            Enter the Tavern
          </Button>
        </div>

        {/* Mode entry chips */}
        <div className="landing__modes">
          {modesForRole(user.role).map(({ mode, icon, label }) => (
            <Chip
              key={mode}
              type="suggestion"
              icon={icon}
              label={label}
              onClick={() => enterWorkspace(mode)}
              className={`landing__mode-chip ${accentClass(mode)}`}
            />
          ))}
        </div>
      </Card>
    </div>
  )
}
