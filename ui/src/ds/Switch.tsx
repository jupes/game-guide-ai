import * as React from 'react'
import './Switch.css'

export interface SwitchProps {
  checked?: boolean
  /** Receives the NEXT boolean value, not a DOM event. */
  onChange?: (next: boolean) => void
  disabled?: boolean
  /** Render a check/close icon inside the handle. */
  icons?: boolean
  ariaLabel?: string
  /** The id of the element that describes this switch (a warning, a reason). */
  ariaDescribedBy?: string
  style?: React.CSSProperties
  className?: string
}

export function Switch({
  checked = false,
  onChange,
  disabled = false,
  icons = false,
  ariaLabel,
  ariaDescribedBy,
  style,
  className,
}: SwitchProps): React.JSX.Element {
  function handleClick() {
    if (!disabled && onChange) {
      onChange(!checked)
    }
  }

  const wrapClasses = [
    'aether-switch-wrap',
    disabled ? 'aether-switch-wrap--disabled' : '',
    className,
  ].filter(Boolean).join(' ')

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-describedby={ariaDescribedBy}
      disabled={disabled}
      onClick={handleClick}
      data-checked={String(checked)}
      data-touch-target="true"
      className={wrapClasses}
      style={style}
    >
      <span className="aether-switch">
        <span className="aether-switch__handle">
          {icons && (
            <span
              className="material-symbols-rounded aether-switch__icon"
              aria-hidden="true"
            >
              {checked ? 'check' : 'close'}
            </span>
          )}
        </span>
      </span>
    </button>
  )
}
