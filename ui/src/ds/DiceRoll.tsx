/**
 * DiceRoll — Aetheril signature dice chip.
 *
 * DS source patches applied here:
 *
 * (a) clip-path clips the border (DS bug):
 *     The hex polygon clip-path is placed on `.dice-roll__pip-bg`, a full-bleed
 *     absolutely-positioned background layer.  The bordered wrapper
 *     `.dice-roll__pip` never carries clip-path and so its 2-px border is
 *     always fully visible.
 *
 * (b) spin animation is hard-coded without reduced-motion guard (DS bug):
 *     The rolling animation class `.dice-roll__pip--spinning` is only added
 *     when `window.matchMedia('(prefers-reduced-motion: reduce)').matches`
 *     is false.  The keyframe itself is also defined inside a
 *     `@media (prefers-reduced-motion: no-preference)` block in DiceRoll.css
 *     so the animation is suppressed at both the CSS and DOM-class level.
 */

import React from 'react'
import './DiceRoll.css'

// ── Props (mirrors the DS d.ts exactly) ──────────────────────────────────────

export interface DiceRollProps {
  /** Die sides. @default 20 */
  die?: number
  /** The rolled face value. */
  value?: number
  /** Flat modifier added to the roll. @default 0 */
  modifier?: number
  /** Caption above the total, e.g. "Stealth check". */
  label?: string
  /** Show the spinning placeholder state. */
  rolling?: boolean
  /** @default 'md' */
  size?: 'sm' | 'md' | 'lg'
  style?: React.CSSProperties
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** True when the browser has no reduced-motion preference.
 *  Falls back to `true` (allow motion) in environments where matchMedia is
 *  unavailable (e.g. jsdom without a stub). */
function motionAllowed(): boolean {
  if (typeof window === 'undefined') return false
  if (typeof window.matchMedia !== 'function') return true
  return !window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

// ── Component ─────────────────────────────────────────────────────────────────

export function DiceRoll({
  die = 20,
  value,
  modifier = 0,
  label,
  rolling = false,
  size = 'md',
  style,
}: DiceRollProps): React.JSX.Element {
  // ── Crit / fumble logic ──────────────────────────────────────────────────
  const isNat20 = die === 20 && value === 20
  const isNat1  = die === 20 && value === 1
  const total   = (value ?? 0) + modifier

  // ── Tone modifier class ──────────────────────────────────────────────────
  const toneClass = isNat20
    ? 'dice-roll--nat20'
    : isNat1
      ? 'dice-roll--nat1'
      : 'dice-roll--normal'

  // ── Spinning class — only when motion is not restricted (bug-b fix) ──────
  const spinClass = rolling && motionAllowed() ? 'dice-roll__pip--spinning' : ''

  // ── Modifier notation — true minus sign − for negatives ─────────────────
  const modNotation = modifier > 0
    ? ` + ${modifier}`
    : modifier < 0
      ? ` − ${Math.abs(modifier)}` // U+2212 MINUS SIGN
      : ''

  return (
    /* style is a DS-contract escape hatch declared in DiceRollProps — not an ad-hoc inline style */
    <div className={`dice-roll dice-roll--${size} ${toneClass}`} style={style}>
      {/* ── Die polygon chip ──────────────────────────────────────────────
          .dice-roll__pip    — bordered ring (no clip-path — bug-a fix)
          .dice-roll__pip-bg — clipped background fill layer               */}
      <div className={`dice-roll__pip ${spinClass}`}>
        <div className="dice-roll__pip-bg" aria-hidden="true" />
        <span className="dice-roll__pip-value">
          {rolling ? '·' : value}
        </span>
      </div>

      {/* ── Info column ────────────────────────────────────────────────── */}
      <div className="dice-roll__info">
        {label && (
          <span className="dice-roll__label">{label}</span>
        )}

        <div className="dice-roll__result-row">
          <span className="dice-roll__total">
            {rolling ? '—' : total}
          </span>
          <span className="dice-roll__notation">
            {`d${die}${modNotation}`}
          </span>
        </div>

        {isNat20 && (
          <span className="dice-roll__crit-badge">CRITICAL!</span>
        )}
        {isNat1 && (
          <span className="dice-roll__crit-badge">FUMBLE</span>
        )}
      </div>
    </div>
  )
}
