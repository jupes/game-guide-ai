import * as React from 'react'
import './TextField.css'

export interface TextFieldProps {
  label?: string
  value?: string
  onChange?: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  /**
   * DS extension: not in the original .d.ts. Forwarded to the underlying
   * input/textarea so consumers can implement Enter-to-send without a wrapper.
   */
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  placeholder?: string
  variant?: 'filled' | 'outlined'
  type?: string
  /** Material Symbols Rounded ligature name shown at the start of the field. */
  leadingIcon?: string
  trailingIcon?: string
  supportingText?: string
  error?: boolean
  disabled?: boolean
  multiline?: boolean
  rows?: number
  fullWidth?: boolean
  style?: React.CSSProperties
  className?: string
}

export function TextField({
  label,
  value,
  onChange,
  onKeyDown,
  placeholder,
  variant = 'outlined',
  type = 'text',
  leadingIcon,
  trailingIcon,
  supportingText,
  error = false,
  disabled = false,
  multiline = false,
  rows = 3,
  fullWidth = false,
  style,
  className,
}: TextFieldProps): React.JSX.Element {
  const [focus, setFocus] = React.useState(false)

  const rootClasses = [
    'aether-field',
    disabled ? 'aether-field--disabled' : '',
    className,
  ].filter(Boolean).join(' ')

  const rowClasses = [
    'aether-field__row',
    multiline ? 'aether-field__row--multiline' : '',
    focus ? 'aether-field__row--focus' : '',
  ].filter(Boolean).join(' ')

  const labelClasses = [
    'aether-field__label',
    error ? 'aether-field__label--error' : '',
    !error && focus ? 'aether-field__label--focus' : '',
  ].filter(Boolean).join(' ')

  const supportClasses = [
    'aether-field__support',
    error ? 'aether-field__support--error' : '',
  ].filter(Boolean).join(' ')

  const trailingIconClasses = [
    'material-symbols-rounded',
    'aether-field__icon',
    error ? 'aether-field__icon--error' : '',
  ].filter(Boolean).join(' ')

  const inputClasses = [
    'aether-field__input',
    multiline ? 'aether-field__input--multiline' : '',
  ].filter(Boolean).join(' ')

  return (
    <div
      className={rootClasses}
      data-variant={variant}
      data-error={error ? 'true' : 'false'}
      data-full-width={fullWidth ? 'true' : undefined}
      data-testid="textfield-root"
      style={style}
    >
      {label && <label className={labelClasses}>{label}</label>}

      <div className={rowClasses}>
        {leadingIcon && (
          <span
            className="material-symbols-rounded aether-field__icon"
            aria-hidden="true"
          >
            {leadingIcon}
          </span>
        )}

        {multiline ? (
          <textarea
            rows={rows}
            value={value}
            onChange={onChange}
            onKeyDown={onKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            onFocus={() => setFocus(true)}
            onBlur={() => setFocus(false)}
            className={inputClasses}
          />
        ) : (
          <input
            type={type}
            value={value}
            onChange={onChange}
            onKeyDown={onKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            onFocus={() => setFocus(true)}
            onBlur={() => setFocus(false)}
            className={inputClasses}
          />
        )}

        {trailingIcon && (
          <span className={trailingIconClasses} aria-hidden="true">
            {trailingIcon}
          </span>
        )}
      </div>

      {supportingText && (
        <span className={supportClasses}>{supportingText}</span>
      )}
    </div>
  )
}
