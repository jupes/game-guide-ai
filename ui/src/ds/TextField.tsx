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
  /**
   * DS extension: not in the original .d.ts (same category as `onKeyDown`
   * above). Multiline only — grows the textarea to fit its content instead of
   * scrolling inside a fixed `rows` box, which is what a chat composer needs.
   *
   * Defaults to **off**, so every existing consumer is byte-for-byte
   * unaffected; opting in is the only way to change behavior. Proposed
   * upstream to aetheril-design-system so the contract converges rather than
   * silently drifting (agent-forge-harness-pp6q.1.4).
   */
  autoGrow?: boolean
  /** Ceiling for `autoGrow`, in px. Past it the textarea scrolls internally. */
  autoGrowMaxPx?: number
  fullWidth?: boolean
  style?: React.CSSProperties
  className?: string
  /**
   * DS extension (record SLASH-12): the underlying control, so a composer can
   * focus it, place the caret and read its value without a wrapper. React 19
   * passes `ref` to a function component as an ordinary prop, so no
   * `forwardRef` is needed.
   */
  ref?: React.Ref<HTMLInputElement | HTMLTextAreaElement>
  onFocus?: (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  onBlur?: (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  /**
   * DS extension (record SLASH-12): ARIA forwarded to the control itself.
   * A textarea that drives a `listbox` needs `aria-autocomplete`,
   * `aria-controls` and `aria-activedescendant` on the textarea — on a wrapper
   * they mean nothing. The set is deliberately small and explicit rather than a
   * prop spread, so the DS still says what it supports.
   */
  'aria-label'?: string
  'aria-describedby'?: string
  'aria-controls'?: string
  'aria-activedescendant'?: string
  'aria-autocomplete'?: 'none' | 'inline' | 'list' | 'both'
  'aria-invalid'?: boolean
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
  autoGrow = false,
  autoGrowMaxPx = 200,
  fullWidth = false,
  style,
  className,
  ref,
  onFocus,
  onBlur,
  'aria-label': ariaLabel,
  'aria-describedby': ariaDescribedby,
  'aria-controls': ariaControls,
  'aria-activedescendant': ariaActivedescendant,
  'aria-autocomplete': ariaAutocomplete,
  'aria-invalid': ariaInvalid,
}: TextFieldProps): React.JSX.Element {
  const [focus, setFocus] = React.useState(false)
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null)

  // One node, two consumers: autoGrow measures it, and the caller holds it.
  const setControlRef = React.useCallback(
    (node: HTMLInputElement | HTMLTextAreaElement | null) => {
      textareaRef.current = node instanceof HTMLTextAreaElement ? node : null
      if (typeof ref === 'function') ref(node)
      else if (ref) ref.current = node
    },
    [ref],
  )

  // autoGrow: measure content, then size to it (pp6q.1.4). Height must be
  // reset before measuring — scrollHeight never reports LESS than the current
  // height, so without the reset the field could only ever grow, never shrink
  // back when the draft is deleted.
  React.useLayoutEffect(() => {
    if (!autoGrow || !multiline) return
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    const next = Math.min(ta.scrollHeight, autoGrowMaxPx)
    ta.style.height = `${next}px`
    ta.style.overflowY = ta.scrollHeight > autoGrowMaxPx ? 'auto' : 'hidden'
  }, [value, autoGrow, autoGrowMaxPx, multiline])
  // Associate the label with its control so assistive tech (and getByLabelText)
  // can resolve it — password/email inputs have no implicit accessible name.
  const controlId = React.useId()

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
      {label && (
        <label htmlFor={controlId} className={labelClasses}>
          {label}
        </label>
      )}

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
            id={controlId}
            ref={setControlRef}
            rows={rows}
            value={value}
            onChange={onChange}
            onKeyDown={onKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            onFocus={(e) => {
              setFocus(true)
              onFocus?.(e)
            }}
            onBlur={(e) => {
              setFocus(false)
              onBlur?.(e)
            }}
            aria-label={ariaLabel}
            aria-describedby={ariaDescribedby}
            aria-controls={ariaControls}
            aria-activedescendant={ariaActivedescendant}
            aria-autocomplete={ariaAutocomplete}
            aria-invalid={ariaInvalid}
            className={inputClasses}
          />
        ) : (
          <input
            id={controlId}
            ref={setControlRef}
            type={type}
            value={value}
            onChange={onChange}
            onKeyDown={onKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            onFocus={(e) => {
              setFocus(true)
              onFocus?.(e)
            }}
            onBlur={(e) => {
              setFocus(false)
              onBlur?.(e)
            }}
            aria-label={ariaLabel}
            aria-describedby={ariaDescribedby}
            aria-controls={ariaControls}
            aria-activedescendant={ariaActivedescendant}
            aria-autocomplete={ariaAutocomplete}
            aria-invalid={ariaInvalid}
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
