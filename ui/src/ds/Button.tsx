import * as React from 'react'
import './Button.css'

export interface ButtonProps {
  ref?: React.Ref<HTMLButtonElement>
  children?: React.ReactNode
  /** Visual emphasis. @default "filled" */
  variant?: 'filled' | 'tonal' | 'elevated' | 'outlined' | 'text'
  /** @default "medium" */
  size?: 'small' | 'medium' | 'large'
  /** Material Symbols Rounded ligature name for a leading icon, e.g. "casino". */
  icon?: string
  /** Material Symbols Rounded ligature name for a trailing icon. */
  trailingIcon?: string
  disabled?: boolean
  fullWidth?: boolean
  type?: 'button' | 'submit' | 'reset'
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void
  style?: React.CSSProperties
  className?: string
  'aria-label'?: string
  'aria-expanded'?: boolean
  'aria-controls'?: string
  'aria-disabled'?: boolean
}

export function Button({
  ref,
  children,
  variant = 'filled',
  size = 'medium',
  icon,
  trailingIcon,
  disabled = false,
  fullWidth = false,
  type = 'button',
  onClick,
  style,
  className,
  'aria-label': ariaLabel,
  'aria-expanded': ariaExpanded,
  'aria-controls': ariaControls,
  'aria-disabled': ariaDisabled,
}: ButtonProps): React.JSX.Element {
  const classes = ['aether-btn', className].filter(Boolean).join(' ')

  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled}
      onClick={onClick}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      aria-controls={ariaControls}
      aria-disabled={ariaDisabled}
      data-variant={variant}
      data-size={size}
      data-full-width={fullWidth ? 'true' : undefined}
      data-touch-target="true"
      className={classes}
      style={style}
    >
      {/* State layer for hover/press ripple */}
      <span className="aether-btn__state" aria-hidden="true" />

      {icon && (
        <span className="material-symbols-rounded" aria-hidden="true">
          {icon}
        </span>
      )}

      <span className="aether-btn__label">{children}</span>

      {trailingIcon && (
        <span className="material-symbols-rounded" aria-hidden="true">
          {trailingIcon}
        </span>
      )}
    </button>
  )
}
