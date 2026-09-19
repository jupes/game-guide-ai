/**
 * SlashMenu (1kg.3.3) — the composer's autocomplete over the shared registry.
 *
 * SLASH-12: the options are **non-focusable elements** with `role="option"`
 * inside a `role="listbox"`, never buttons — focus stays in the composer, which
 * drives the menu through `aria-activedescendant`. The handoff's buttons would
 * have taken focus out of the textarea on every arrow key.
 *
 * SLASH-9: zero matches render one inert row rather than vanishing, so the menu
 * answers "there is no such tool" instead of leaving the GM to guess.
 * SLASH-10: a disabled tool is listed, can be the active option so its reason is
 * reachable, and cannot be accepted.
 *
 * Presentational and controlled: `useSlashMenu` owns which option is active.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import type { Tool, ToolAvailability } from './registry'
import './SlashMenu.css'

export interface SlashMenuProps {
  /** The listbox's id — the composer's `aria-controls` target. */
  id: string
  /** The typed token, for the no-match row. */
  token: string
  /** SLASH-9: registry order, already prefix-matched. */
  options: readonly Tool[]
  availability: Readonly<Record<ToolId, ToolAvailability>>
  activeIndex: number
  optionId: (index: number) => string
  onPick: (tool: Tool) => void
  /** §4.4: hover moves the active option. */
  onHover: (index: number) => void
  className?: string
}

export function SlashMenu({
  id,
  token,
  options,
  availability,
  activeIndex,
  optionId,
  onPick,
  onHover,
  className,
}: SlashMenuProps): React.JSX.Element {
  const classes = ['gm-slash-menu', className].filter(Boolean).join(' ')

  return (
    <div
      className={classes}
      // §4.4: the pointer must not take focus off the composer, or the menu
      // would close under the very click that picks an option.
      onMouseDown={(e) => e.preventDefault()}
    >
      <p className="gm-slash-menu__heading" aria-hidden="true">
        Tools
      </p>
      <ul id={id} role="listbox" aria-label="Tools" className="gm-slash-menu__list">
        {options.length === 0 ? (
          <li role="option" aria-selected={false} aria-disabled="true" className="gm-slash-menu__empty">
            {`No tool matches "/${token}"`}
          </li>
        ) : (
          options.map((tool, index) => {
            const state = availability[tool.id]
            const disabled = state?.enabled !== true
            const active = index === activeIndex
            return (
              <li
                key={tool.id}
                id={optionId(index)}
                role="option"
                aria-selected={active}
                aria-disabled={disabled || undefined}
                data-active={active ? 'true' : undefined}
                className="gm-slash-menu__option"
                onMouseEnter={() => onHover(index)}
                onClick={() => {
                  if (!disabled) onPick(tool)
                }}
              >
                <span className="material-symbols-rounded gm-slash-menu__icon" aria-hidden="true">
                  {tool.icon}
                </span>
                <span className="gm-slash-menu__text">
                  <span className="gm-slash-menu__command">{tool.command}</span>
                  <span className="gm-slash-menu__blurb">{tool.blurb}</span>
                  {disabled && state?.reason && <span className="gm-slash-menu__reason">{state.reason}</span>}
                </span>
                {active && !disabled && (
                  <span className="gm-slash-menu__enter" aria-hidden="true">
                    ↵
                  </span>
                )}
              </li>
            )
          })
        )}
      </ul>
    </div>
  )
}
