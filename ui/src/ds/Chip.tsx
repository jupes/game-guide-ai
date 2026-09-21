/**
 * Chip — Material 3 chip.
 *
 * Behavior #7:
 *  - Types: assist (default), filter, input, suggestion
 *  - Selected filter chip fills with ember/gold (secondary-container)
 *  - 8px radius via --aether-radius-chip token
 *  - Optional leading icon (Material Symbols Rounded ligature)
 *  - onClick fires on click; onRemove fires when input chip close is clicked
 *  - Disabled suppresses onClick
 */

import React from 'react'
import './Chip.css'

export interface ChipProps {
  label: string
  type?: 'assist' | 'filter' | 'input' | 'suggestion'
  /** Material Symbols Rounded ligature name. */
  icon?: string
  /** Selected state for filter chips. */
  selected?: boolean
  onClick?: () => void
  /** Called when the remove (×) affordance of an input chip is clicked. */
  onRemove?: () => void
  disabled?: boolean
  style?: React.CSSProperties
  className?: string
  [key: string]: unknown
}

export function Chip({
  label,
  type = 'assist',
  icon,
  selected = false,
  onClick,
  onRemove,
  disabled = false,
  style,
  className = '',
  ...rest
}: ChipProps) {
  const isFilterSelected = type === 'filter' && selected

  const classes = [
    'chip',
    type === 'input' ? 'chip--input' : '',
    isFilterSelected ? 'chip--selected' : '',
    disabled ? 'chip--disabled' : '',
    onClick && !disabled ? 'chip--clickable' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  function handleClick() {
    if (!disabled && onClick) {
      onClick()
    }
  }

  function handleRemove(e: React.MouseEvent) {
    e.stopPropagation()
    if (onRemove) {
      onRemove()
    }
  }

  // Interactive chips must be keyboard-reachable and expose a button role so
  // assistive technology announces them correctly (a11y fix — CP-F6.1).
  //
  // agent-forge-harness-27h: a selected FILTER chip is also a pressed toggle,
  // and it now says so. Until this, the only evidence that (say) the Rules
  // channel was the open one was a fill colour and a check glyph that is
  // aria-hidden — so a screen-reader user could switch channels but never hear
  // which one they were in. The AppHeader and LeftNav channel switchers are
  // built from these chips. No axe rule covers a missing selected state; the
  // shell stories are what found it.
  const isInteractive = Boolean(onClick) && !disabled
  const interactiveProps = isInteractive
    ? {
        role: 'button' as const,
        tabIndex: 0,
        ...(type === 'filter' ? { 'aria-pressed': selected } : {}),
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            handleClick()
          }
        },
      }
    : {}

  return (
    <div
      className={classes}
      onClick={handleClick}
      style={style}
      {...rest}
      {...interactiveProps}
    >
      <span className="chip__state-layer" aria-hidden="true" />

      {/* Check icon when filter is selected */}
      {isFilterSelected && (
        <span className="material-symbols-rounded chip__icon" aria-hidden="true">
          check
        </span>
      )}

      {/* Leading icon (hidden when filter-selected shows check instead) */}
      {icon && !isFilterSelected && (
        <span className="material-symbols-rounded chip__icon" aria-hidden="true">
          {icon}
        </span>
      )}

      <span className="chip__label">{label}</span>

      {/* Remove affordance for input chips */}
      {type === 'input' && (
        <span
          role="button"
          aria-label="Remove"
          onClick={handleRemove}
          className="material-symbols-rounded chip__remove"
        >
          close
        </span>
      )}
    </div>
  )
}
