/**
 * LossGuardDialog -- the unsaved-changes dialog (agent-forge-harness-1kg.6.3;
 * interactions ADR §5.3, CANVAS-16, AE-47, LAYOUT-10).
 *
 * It appears only when a flush failed, timed out or left a conflict (the pure
 * decision is `lossGuard.ts`'s). It is props-driven: the canvas provider owns when
 * it opens and what each button resolves.
 *
 * - A modal `role="dialog"` named by its heading, `You have unsaved changes to
 *   <title>`.
 * - **Keep editing** takes focus on open and is what Escape does: the safe choice
 *   is the default, and nothing is discarded by accident.
 * - **Try saving again** shows only when a retry can help.
 * - **Discard changes** is the one destructive button.
 * - Tab wraps inside it (`focusTrap.ts`, the same wrap the drawer and
 *   CustomiseRailDialog use), and focus returns to whatever had it before.
 * - The shell makes the chrome, rail and body `inert` while it is open (it renders
 *   at the shell root, outside every inert container).
 */

import * as React from 'react'
import { wrapTab } from './focusTrap'
import { WORKBENCH_COPY } from './workbenchCopy'
import './LossGuardDialog.css'

export interface LossGuardDialogProps {
  /** The document's title, GM-private: shown, never stored. */
  title: string
  /** Whether trying again can help (every unsaved source said `retryable`). */
  retryable: boolean
  /** A retry is running: it cannot be pressed again, and nothing can be discarded meanwhile. */
  busy: boolean
  onKeep: () => void
  onRetry: () => void
  onDiscard: () => void
}

export function LossGuardDialog({
  title,
  retryable,
  busy,
  onKeep,
  onRetry,
  onDiscard,
}: LossGuardDialogProps): React.JSX.Element {
  const id = React.useId()
  const headingId = `${id}-heading`
  const bodyId = `${id}-body`
  const dialogRef = React.useRef<HTMLDivElement>(null)
  const keepRef = React.useRef<HTMLButtonElement>(null)

  React.useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    keepRef.current?.focus()
    return () => {
      if (opener !== null && opener.isConnected) opener.focus()
    }
  }, [])

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      // Consumed here, so the nav drawer's document-level Escape does not also close.
      event.preventDefault()
      onKeep()
      return
    }
    if (dialogRef.current !== null) wrapTab(event, dialogRef.current)
  }

  return (
    <div className="loss-guard__scrim">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        aria-describedby={bodyId}
        tabIndex={-1}
        className="loss-guard"
        onKeyDown={handleKeyDown}
      >
        <h2 id={headingId} className="loss-guard__heading">
          {WORKBENCH_COPY.guardHeading(title)}
        </h2>
        <p id={bodyId} className="loss-guard__body">
          {retryable ? WORKBENCH_COPY.guardBodyRetryable : WORKBENCH_COPY.guardBody}
        </p>
        <div className="loss-guard__actions">
          <button ref={keepRef} type="button" className="loss-guard__button loss-guard__button--primary" onClick={onKeep}>
            {WORKBENCH_COPY.keepEditing}
          </button>
          {retryable && (
            <button type="button" className="loss-guard__button" disabled={busy} onClick={onRetry}>
              {busy ? WORKBENCH_COPY.savingAgain : WORKBENCH_COPY.trySavingAgain}
            </button>
          )}
          <button
            type="button"
            className="loss-guard__button loss-guard__button--destructive"
            disabled={busy}
            onClick={onDiscard}
          >
            {WORKBENCH_COPY.discardChanges}
          </button>
        </div>
      </div>
    </div>
  )
}
