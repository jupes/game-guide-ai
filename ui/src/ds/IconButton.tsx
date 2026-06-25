import * as React from 'react'
import './IconButton.css'

export interface IconButtonProps {
  /** Material Symbols Rounded ligature name, e.g. "menu", "send", "casino". */
  icon: string
  variant?: 'standard' | 'filled' | 'tonal' | 'outlined'
  size?: 'small' | 'medium' | 'large'
  /** Toggle-selected state (colors the standard variant with primary). */
  selected?: boolean
  disabled?: boolean
  /** Accessible label — required since the button has no text. */
  ariaLabel?: string
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void
  style?: React.CSSProperties
  className?: string
}

export function IconButton({
  icon,
  variant = 'standard',
  size = 'medium',
  selected = false,
  disabled = false,
  ariaLabel,
  onClick,
  style,
  className,
}: IconButtonProps): React.JSX.Element {
  const classes = ['aether-icon-btn', className].filter(Boolean).join(' ')

  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      data-variant={variant}
      data-size={size}
      data-touch-target="true"
      className={classes}
      style={style}
    >
      {/* State layer for hover/press ripple */}
      <span className="aether-icon-btn__state" aria-hidden="true" />

      <span className="material-symbols-rounded" aria-hidden="true">
        {icon}
      </span>
    </button>
  )
}
