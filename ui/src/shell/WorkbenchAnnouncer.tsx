/**
 * WorkbenchAnnouncer -- the Workbench's one polite status node
 * (agent-forge-harness-1kg.6.3; A-29, I-16, Critic C-11).
 *
 * Canvas-level outcomes that no focus move explains are spoken here and nowhere
 * else: `Couldn't open <title>. Nothing changed.`, the offline refusal, and a late
 * failure panel while the GM kept typing. ChatPane's own announcer and the canvas
 * pane's save status are untouched, and loading text is visible, not announced.
 *
 * It is mounted EMPTY for the Workbench's life (a live region that appears together
 * with its text is not reliably announced) and rendered at the shell root, outside
 * every container the shell makes `inert`, so a message is heard even while the
 * loss-guard dialog or the drawer is open.
 */

import * as React from 'react'
import { announcementText, useCanvasState } from './canvasContext'
import './WorkbenchAnnouncer.css'

export function WorkbenchAnnouncer(): React.JSX.Element {
  const state = useCanvasState()
  return (
    <p role="status" className="workbench-announcer">
      {announcementText(state)}
    </p>
  )
}
