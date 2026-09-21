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
  return (
    <div
      className="gm-selection-bar"
      role="toolbar"
      aria-label={`Ask the assistant about the selected text in ${selection.label}`}
      data-placement={placement.placement}
      style={{ left: `${placement.left}px`, top: `${placement.top}px` }}
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return
        // The owner returns focus to the field: this bar is about to be gone,
        // and a control that is unmounting cannot decide where focus lands.
        event.stopPropagation()
        onDismiss()
      }}
    >
      <span className="material-symbols-rounded gm-selection-bar__mark" aria-hidden="true">
        auto_fix_high
      </span>
      {EDIT_ACTIONS.map((action) => (
        <button
          key={action}
          type="button"
          className="gm-selection-bar__action"
          onClick={() => onAction(action, selection)}
        >
          {ACTION_LABELS[action]}
        </button>
      ))}
    </div>
  )
}
