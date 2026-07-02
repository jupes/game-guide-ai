/**
 * Avatar — round portrait for players, characters, and the DM.
 *
 * Behavior #8:
 *  - Initials: derived from `name` — first letter of each word (max 2), uppercased
 *  - Icon variant: when `icon` is given and `src` is absent
 *  - Image variant: when `src` is given (background-image; no initials/icon rendered)
 *  - Tones: gold (default), ember, verdigris, arcane
 *  - `ring` adds a gilt ring (active speaker / DM indicator)
 *  - `size` defaults to 40px (width, height, proportional font-size)
 *  - DM role: ember tone + icon
 */

import React from 'react'
import './Avatar.css'

export interface AvatarProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Image URL. When absent, falls back to icon or initials. */
  src?: string
  /** Full name — first letters become the initials fallback. */
  name?: string
  /** Material Symbols Rounded ligature name used instead of initials. */
  icon?: string
  /** Pixel diameter. @default 40 */
  size?: number
  tone?: 'gold' | 'ember' | 'verdigris' | 'arcane'
  /** Adds a gilt ring (use for the active speaker / DM). */
  ring?: boolean
}

/** Derive up to 2 uppercase initials from a full name string. */
// eslint-disable-next-line react-refresh/only-export-components -- helper co-located with Avatar; HMR-only rule
export function deriveInitials(name: string): string {
  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((word) => word[0])
    .filter(Boolean)
    .join('')
    .toUpperCase()
}

export function Avatar({
  src,
  name = '',
  icon,
  size = 40,
  tone = 'gold',
  ring = false,
  style,
  className = '',
  ...rest
}: AvatarProps) {
  const initials = deriveInitials(name)

  const classes = [
    'avatar',
    `avatar--${tone}`,
    ring ? 'avatar--ring' : '',
    src ? 'avatar--image' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  // width/height/font-size are genuinely dynamic (driven by the size prop);
  // everything else lives in Avatar.css.
  const inlineStyle: React.CSSProperties = {
    width: size,
    height: size,
    fontSize: size * 0.4,
    ...(src ? { backgroundImage: `url(${src})` } : {}),
    ...style,
  }

  return (
    <div className={classes} style={inlineStyle} {...rest}>
      {!src && (
        icon
          ? (
            <span
              className="material-symbols-rounded avatar__icon"
              aria-hidden="true"
            >
              {icon}
            </span>
          )
          : initials
      )}
    </div>
  )
}
