/**
 * Badge — small status / count indicator with Aetheril semantic tones.
 *
 * Behavior #9:
 *  - Semantic tones: primary (default), neutral, gold, verdigris, arcane, error, nat20
 *  - Extension: `nat1` tone (critical failure, colored via --aether-nat1) — not in upstream .d.ts
 *  - dot mode: renders as 8px circle dot, ignores children
 *  - Supports ALL-CAPS dice language labels (NAT 20, NAT 1, D20, etc.)
 */

import React from 'react'
import './Badge.css'

/** Tone values from the DS spec plus the `nat1` extension. */
export type BadgeTone =
  | 'primary'
  | 'neutral'
  | 'gold'
  | 'verdigris'
  | 'arcane'
  | 'error'
  | 'nat20'
  | 'nat1'  // Extension: critical failure — not in upstream .d.ts

export interface BadgeProps {
  children?: React.ReactNode
  tone?: BadgeTone
  /** Render as a bare 8px dot (ignores children). */
  dot?: boolean
  style?: React.CSSProperties
  className?: string
  [key: string]: unknown
}

export function Badge({
  children,
  tone = 'primary',
  dot = false,
  style,
  className = '',
  ...rest
}: BadgeProps) {
  const classes = [
    'badge',
    `badge--${tone}`,
    dot ? 'badge--dot' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <span className={classes} style={style} {...rest}>
      {!dot ? children : null}
    </span>
  )
}
