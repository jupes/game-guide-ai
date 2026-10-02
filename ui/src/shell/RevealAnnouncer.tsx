/**
 * RevealAnnouncer -- the one polite status node for reveal outcomes
 * (agent-forge-harness-1kg.7.3, brief 7; A-29).
 *
 * `Shown to the table`, `Updated what Brann sees`, `Stopped showing <title>`, and the
 * Stop that is still retrying: spoken here and nowhere else, because no focus move
 * explains them (a Confirm closes the sheet, a Stop moves nothing).
 *
 * It is mounted EMPTY for the Workbench's life, a sibling of `WorkbenchAnnouncer`, and
 * rendered at the shell root, outside every container the shell makes `inert`, so a
 * message is heard even while the sheet's own dialog is open. Never inside a column or
 * the dialog. It reuses the Workbench announcer's visually-hidden style.
 */

import * as React from 'react'
import { revealAnnouncementText, useReveals } from './revealContext'
import './WorkbenchAnnouncer.css'

export function RevealAnnouncer(): React.JSX.Element {
  const reveals = useReveals()
  return (
    <p role="status" className="workbench-announcer" data-reveal-announcer="">
      {revealAnnouncementText(reveals)}
    </p>
  )
}
