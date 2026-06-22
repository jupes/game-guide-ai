/**
 * Card — Material 3 card surface — warm paper with umber shadow.
 *
 * Behavior #6:
 *  - Variants: elevated (default), filled, outlined
 *  - `interactive` adds hover elevation lift + state layer
 *  - 16px radius via --aether-radius-card token
 *  - 24px default padding via --aether-card-padding token
 *  - Optional onClick handler
 */

import React from 'react'
import './Card.css'

export interface CardProps {
  children?: React.ReactNode
  variant?: 'elevated' | 'filled' | 'outlined'
  /** Adds hover elevation + state layer; pair with onClick. */
  interactive?: boolean
  onClick?: (e: React.MouseEvent<HTMLDivElement>) => void
  /** Apply default 24px padding. @default true */
  padded?: boolean
  style?: React.CSSProperties
  className?: string
  [key: string]: unknown
}

export function Card({
  children,
  variant = 'elevated',
  interactive = false,
  onClick,
  padded = true,
  style,
  className = '',
  ...rest
}: CardProps) {
  const classes = [
    'card',
    `card--${variant}`,
    padded ? 'card--padded' : '',
    interactive ? 'card--interactive' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div
      className={classes}
      onClick={onClick}
      style={style}
      {...rest}
    >
      {interactive && <span className="card__state-layer" aria-hidden="true" />}
      {children}
    </div>
  )
}
