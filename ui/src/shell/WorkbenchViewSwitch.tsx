/**
 * WorkbenchViewSwitch -- Chat / Canvas (agent-forge-harness-1kg.6.3; LAYOUT-5, I-4,
 * Critic C-10).
 *
 * Below 1024 px the workspace shows one column at a time: the chat or the canvas.
 * This group of two toggle buttons picks which. It is a row in the shell chrome,
 * directly under the channels (LAYOUT-5's "workspace header"), so it goes inert
 * with the chrome while the drawer or the loss-guard dialog is open.
 *
 * The buttons are native, with `aria-pressed`, 44 px tall; the selected segment
 * is tonal (`secondary-container`). Pressing one moves focus nowhere: the pressed
 * segment keeps it (LAYOUT-6). There are no news dots yet (I-4): they are a scope
 * choice, and the owner may ask for the Chat dot.
 */

import * as React from 'react'
import type { WorkbenchView } from './canvasContext'
import { WORKBENCH_COPY } from './workbenchCopy'
import './WorkbenchViewSwitch.css'

export interface WorkbenchViewSwitchProps {
  view: WorkbenchView
  onChange: (view: WorkbenchView) => void
}

const SEGMENTS: ReadonlyArray<{ view: WorkbenchView; label: string }> = [
  { view: 'chat', label: WORKBENCH_COPY.viewChat },
  { view: 'canvas', label: WORKBENCH_COPY.viewCanvas },
]

export function WorkbenchViewSwitch({ view, onChange }: WorkbenchViewSwitchProps): React.JSX.Element {
  return (
    <div className="workbench-switch" role="group" aria-label={WORKBENCH_COPY.viewSwitchLabel}>
      {SEGMENTS.map((segment) => (
        <button
          key={segment.view}
          type="button"
          className="workbench-switch__segment"
          aria-pressed={view === segment.view}
          onClick={() => onChange(segment.view)}
        >
          {segment.label}
        </button>
      ))}
    </div>
  )
}
