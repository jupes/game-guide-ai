/**
 * ToolMenu — the rail's **More** (1kg.3.3) — SLASH-13, SLASH-14, RAIL-10.
 *
 * A menu button: `aria-haspopup="menu"` and `aria-expanded`, a `role="menu"` of
 * `role="menuitem"` rows, and the menu pattern's focus contract — opening moves
 * focus to the first item, arrows wrap, Home and End jump, Enter or Space
 * activates, Escape and Tab close and return focus to the button.
 *
 * A pick is the one case that does NOT return focus here: it arms the composer,
 * and typing the brief is the next step, so `onArm` moves focus there (SLASH-13).
 *
 * SLASH-14: the unpinned tools by blurb, then a separator, then Customise rail…
 * RAIL-10: a capability-disabled tool is listed, is reachable so a screen reader
 * hears its reason, and arms nothing.
 */

import * as React from 'react'
import type { ToolId } from './contracts'
import type { Tool, ToolAvailability } from './registry'
import './ToolMenu.css'

export interface ToolMenuProps {
  /** SLASH-14: the unpinned tools, from `overflow(pins)`. */
  tools: readonly Tool[]
  availability: Readonly<Record<ToolId, ToolAvailability>>
  /** RAIL-1: arms the composer. It never invokes, and it moves focus to the composer. */
  onArm: (tool: Tool) => void
  /** SLASH-15: opens the Customise rail dialog. Focus is already back on the button. */
  onCustomise?: () => void
  /** RAIL-13: no campaign selected — the whole rail, More included, is inert. */
  disabled?: boolean
  className?: string
}

interface MenuRow {
  key: string
  label: string
  icon: string
  reason: string | null
  activate: (() => void) | null
  /** True when picking it hands focus to the composer instead of the button. */
  handsOffFocus: boolean
}

export function ToolMenu({
  tools,
  availability,
  onArm,
  onCustomise,
  disabled = false,
  className,
}: ToolMenuProps): React.JSX.Element {
  const id = React.useId()
  const menuId = `${id}-more-menu`
  const [open, setOpen] = React.useState(false)
  const [activeIndex, setActiveIndex] = React.useState(0)
  const buttonRef = React.useRef<HTMLButtonElement>(null)
  const itemsRef = React.useRef<(HTMLButtonElement | null)[]>([])
  const rootRef = React.useRef<HTMLDivElement>(null)

  const rows: MenuRow[] = React.useMemo(() => {
    const toolRows: MenuRow[] = tools.map((tool) => {
      const state = availability[tool.id]
      const toolDisabled = state?.enabled !== true
      return {
        key: tool.id,
        label: tool.blurb || tool.label,
        icon: tool.icon,
        reason: toolDisabled ? (state?.reason ?? 'That tool is unavailable.') : null,
        activate: toolDisabled ? null : () => onArm(tool),
        handsOffFocus: !toolDisabled,
      }
    })
    if (!onCustomise) return toolRows
    return [
      ...toolRows,
      { key: 'customise', label: 'Customise rail…', icon: 'tune', reason: null, activate: onCustomise, handsOffFocus: false },
    ]
  }, [availability, onArm, onCustomise, tools])

  const closeAndReturnFocus = React.useCallback(() => {
    setOpen(false)
    buttonRef.current?.focus()
  }, [])

  // Opening moves focus to the first item (SLASH-13); every later move focuses
  // the item the arrows landed on, which is the menu pattern's roving focus.
  React.useEffect(() => {
    if (!open) return
    itemsRef.current[activeIndex]?.focus()
  }, [open, activeIndex])

  // An outside pointer closes the menu. Focus is left wherever the browser puts
  // it: the GM aimed at something else, and yanking focus back to More would
  // fight that. Escape and Tab — the keyboard closes — do return it.
  React.useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  function toggle() {
    if (disabled) return
    setActiveIndex(0)
    setOpen((was) => !was)
  }

  function activate(index: number) {
    const row = rows[index]
    if (!row?.activate) return
    if (row.handsOffFocus) {
      // The composer takes focus (SLASH-13), so nothing here may grab it back.
      setOpen(false)
      row.activate()
      return
    }
    closeAndReturnFocus()
    row.activate()
  }

  function handleMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const count = rows.length
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        setActiveIndex((index) => (index + 1) % count)
        break
      case 'ArrowUp':
        event.preventDefault()
        setActiveIndex((index) => (index - 1 + count) % count)
        break
      case 'Home':
        event.preventDefault()
        setActiveIndex(0)
        break
      case 'End':
        event.preventDefault()
        setActiveIndex(count - 1)
        break
      case 'Escape':
        event.preventDefault()
        closeAndReturnFocus()
        break
      case 'Tab':
        // Not prevented: focus returns to the button and the browser's own Tab
        // carries on from there, so the menu never swallows the tab order.
        closeAndReturnFocus()
        break
      default:
        break
    }
  }

  return (
    <div className={['gm-tool-menu', className].filter(Boolean).join(' ')} ref={rootRef}>
      <button
        type="button"
        ref={buttonRef}
        className="gm-tool-menu__button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-disabled={disabled || undefined}
        data-touch-target="true"
        onClick={toggle}
      >
        <span className="material-symbols-rounded gm-tool-menu__button-icon" aria-hidden="true">
          more_horiz
        </span>
        More
      </button>

      {open && (
        <div id={menuId} role="menu" aria-label="More tools" className="gm-tool-menu__menu" onKeyDown={handleMenuKeyDown}>
          {rows.map((row, index) => (
            <React.Fragment key={row.key}>
              {row.key === 'customise' && <div role="separator" className="gm-tool-menu__separator" />}
              <button
                type="button"
                role="menuitem"
                ref={(node) => {
                  itemsRef.current[index] = node
                }}
                tabIndex={index === activeIndex ? 0 : -1}
                aria-disabled={row.activate === null || undefined}
                aria-describedby={row.reason ? `${id}-reason-${row.key}` : undefined}
                className="gm-tool-menu__item"
                onFocus={() => setActiveIndex(index)}
                onClick={() => activate(index)}
              >
                <span className="material-symbols-rounded gm-tool-menu__icon" aria-hidden="true">
                  {row.icon}
                </span>
                <span className="gm-tool-menu__label">{row.label}</span>
                {row.reason && (
                  <span id={`${id}-reason-${row.key}`} className="gm-tool-menu__reason">
                    {row.reason}
                  </span>
                )}
              </button>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  )
}
