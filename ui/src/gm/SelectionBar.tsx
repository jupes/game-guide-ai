/**
 * SelectionBar (agent-forge-harness-1kg.6.2) — CANVAS-23's targeted edit.
 *
 * `Rewrite`, `Shorter` and `Darker` are complete requests: they run on click,
 * scoped to the selection, and this component **reports** them. It never
 * fetches, never arms and never spends anything — the owner decides (X-1).
 *
 * What it carries is exactly what `EditScope`'s selection shape needs: the
 * field key, the span in CODE POINTS and the text the GM saw. The server
 * refuses a span that no longer matches before any provider work (`1kg.5.5`),
 * which is why the text travels with the offsets rather than being re-read.
 *
 * Placement is computed by `selectionBarPosition` and passed in, so that the
 * geometry — clear of the selection, inside the pane at 400 px — is testable
 * without a browser. The bar is rendered inside its field, so it follows that
 * field in the tab order; the containing block is the document root, which is
 * why the coordinates are the root's.
 */

import * as React from 'react'

import { EDIT_ACTIONS } from './contracts'
import type { BarPlacement } from './documentFields'
import './SelectionBar.css'

export type EditAction = (typeof EDIT_ACTIONS)[number]

/** One selection inside one field. Never across two (CANVAS-23). */
export interface DocumentSelection {
  field: string
  /** The registry's label for that field, for the bar's accessible name. */
  label: string
  /** Code points, not UTF-16 units — the unit `EditScope` counts in. */
  start: number
  end: number
  /** Exactly the text the GM saw, which the server checks the span against. */
  text: string
}

const ACTION_LABELS: Readonly<Record<EditAction, string>> = {
  rewrite: 'Rewrite',
  shorter: 'Shorter',
  darker: 'Darker',
}

export interface SelectionBarProps {
  selection: DocumentSelection
  /** From `selectionBarPosition`, in the document root's coordinates. */
  placement: BarPlacement
  onAction: (action: EditAction, selection: DocumentSelection) => void
  /** Escape, or the owner deciding the selection is gone. */
  onDismiss: () => void
}

export function SelectionBar({ selection, placement, onAction, onDismiss }: SelectionBarProps): React.JSX.Element {
  const host = React.useRef<HTMLDivElement>(null)
  /**
   * The roving tab stop. `role="toolbar"` is a promise to a keyboard user —
   * one stop on the way in, arrow keys between the actions — and a bar with
   * three tab stops and no arrow keys makes the promise without keeping it.
   * The bar sits between a field's text and whatever follows it, so three
   * stops also means three extra presses to get past it on every selection.
   */
  const [active, setActive] = React.useState(0)

  function focusAction(index: number): void {
    const wrapped = (index + EDIT_ACTIONS.length) % EDIT_ACTIONS.length
    setActive(wrapped)
    host.current?.querySelectorAll<HTMLElement>('.gm-selection-bar__action')[wrapped]?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent): void {
    if (event.key === 'Escape') {
      // The owner returns focus to the field: this bar is about to be gone,
      // and a control that is unmounting cannot decide where focus lands.
      event.stopPropagation()
      onDismiss()
      return
    }
    const step =
      event.key === 'ArrowRight' ? active + 1 : event.key === 'ArrowLeft' ? active - 1 : null
    const jump = event.key === 'Home' ? 0 : event.key === 'End' ? EDIT_ACTIONS.length - 1 : null
    const next = step ?? jump
    if (next === null) return
    event.preventDefault()
    // Arrows stay inside the bar; Tab is what leaves it, which is why the
    // wrap below can never become a focus trap.
    event.stopPropagation()
    focusAction(next)
  }

  return (
    <div
      className="gm-selection-bar"
      ref={host}
      role="toolbar"
      aria-label={`Ask the assistant about the selected text in ${selection.label}`}
      data-placement={placement.placement}
      style={{ left: `${placement.left}px`, top: `${placement.top}px` }}
      onKeyDown={onKeyDown}
    >
      <span className="material-symbols-rounded gm-selection-bar__mark" aria-hidden="true">
        auto_fix_high
      </span>
      {EDIT_ACTIONS.map((action, index) => (
        <button
          key={action}
          type="button"
          className="gm-selection-bar__action"
          tabIndex={index === active ? 0 : -1}
          // A pointer can land on any of them, and the tab stop follows.
          onFocus={() => setActive(index)}
          onClick={() => onAction(action, selection)}
        >
          {ACTION_LABELS[action]}
        </button>
      ))}
    </div>
  )
}
