/**
 * TavernActionButton -- the design system's button markup with the ARIA the DS
 * `Button` does not spread (agent-forge-harness-30c, PR-1).
 *
 * The DS `Button` takes no `aria-*`, and the tavern needs four of them: a
 * locked Start Session that stays focusable and says why (`aria-disabled` plus
 * `aria-describedby`), and the Concluded disclosure (`aria-expanded` plus
 * `aria-controls`). The markup is the DS's exactly -- `aether-btn`,
 * `data-variant`, `data-size`, `data-touch-target`, the `__state` layer and
 * `aria-hidden` icon spans -- so the 44px floor and both themes come from the
 * DS stylesheet, and nothing here sizes a button (`ds/touchTargetLeak.test.ts`).
 * Pattern: `CampaignPicker`'s `PendingButton`.
 */

import * as React from 'react'
import '../ds/Button.css'

export interface TavernActionButtonProps {
  children: React.ReactNode
  variant: 'filled' | 'tonal' | 'elevated' | 'outlined' | 'text'
  /** Material Symbols Rounded ligature for a leading icon. */
  icon?: string
  /** The same, trailing. */
  trailingIcon?: string
  /** Locked: still focusable and described, and a press does nothing. */
  ariaDisabled?: boolean
  ariaExpanded?: boolean
  ariaControls?: string
  ariaDescribedBy?: string
  onPress: () => void
  className?: string
  /** For a host that moves focus here (the Concluded toggle, after a conclude). */
  buttonRef?: React.Ref<HTMLButtonElement>
}

export function TavernActionButton({
  children,
  variant,
  icon,
  trailingIcon,
  ariaDisabled = false,
  ariaExpanded,
  ariaControls,
  ariaDescribedBy,
  onPress,
  className,
  buttonRef,
}: TavernActionButtonProps): React.JSX.Element {
  return (
    <button
      ref={buttonRef}
      type="button"
      className={['aether-btn', className].filter(Boolean).join(' ')}
      data-variant={variant}
      data-size="medium"
      data-touch-target="true"
      aria-disabled={ariaDisabled || undefined}
      aria-expanded={ariaExpanded}
      aria-controls={ariaControls}
      aria-describedby={ariaDescribedBy}
      onClick={() => {
        if (!ariaDisabled) onPress()
      }}
    >
      <span className="aether-btn__state" aria-hidden="true" />
      {icon !== undefined && <span className="material-symbols-rounded" aria-hidden="true">{icon}</span>}
      <span className="aether-btn__label">{children}</span>
      {trailingIcon !== undefined && <span className="material-symbols-rounded" aria-hidden="true">{trailingIcon}</span>}
    </button>
  )
}
