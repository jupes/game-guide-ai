/**
 * LibraryDialog -- the Campaign Library's one modal (agent-forge-harness-1kg.6.4;
 * LIB-12, LIB-17, LIB-18, LAYOUT-10).
 *
 * The same focus-trap pattern as `CustomiseRailDialog` and `LossGuardDialog`: a
 * `role="dialog"` named by its heading, `aria-modal`, Tab wrapped inside (`focusTrap`),
 * Escape cancels, and focus returns to whatever had it when the dialog opened.
 *
 * - Three callers: the stat-block New form (initial focus on its first field), the
 *   archive-while-revealed confirmation (initial focus on Cancel, the safe choice) and the
 *   delete confirmation (initial focus on the password field).
 * - It is a `<form>`, so Enter in a field submits. While a request is in flight the
 *   confirm button is `aria-disabled` (a disabled button drops focus) and Cancel and Escape
 *   do nothing, so an answer never lands on a dialog the GM thinks is gone.
 * - A refusal appears in a `role="alert"` inside the dialog: the status node of the panel
 *   is outside an `aria-modal` surface and is not reliably read from here.
 * - Nothing it shows or holds goes anywhere: a title is GM-private text (X-7) and a
 *   password lives only in the caller's state, which unmounts with the dialog.
 */

import * as React from 'react'
import { wrapTab } from './focusTrap'
import './LibraryDialog.css'

export interface LibraryDialogProps {
  heading: string
  body?: string
  /** Fields, placed between the body and the actions. */
  children?: React.ReactNode
  confirmLabel: string
  cancelLabel: string
  /** The request is in flight. */
  busy: boolean
  destructive?: boolean
  /** Which control takes focus when the dialog opens: the field `fieldRef` names, or Cancel. */
  initialFocus: 'field' | 'cancel'
  fieldRef?: React.RefObject<HTMLElement | null>
  /** A refusal, shown and read out inside the dialog. */
  error?: string | null
  onConfirm: () => void
  onCancel: () => void
}

export function LibraryDialog({
  heading, body, children, confirmLabel, cancelLabel, busy, destructive = false, initialFocus, fieldRef, error = null, onConfirm, onCancel,
}: LibraryDialogProps): React.JSX.Element {
  const id = React.useId()
  const headingId = `${id}-heading`
  const bodyId = `${id}-body`
  const dialogRef = React.useRef<HTMLDivElement>(null)
  const cancelRef = React.useRef<HTMLButtonElement>(null)
  const focusOnOpen = React.useRef({ initialFocus, fieldRef })

  React.useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const { initialFocus: first, fieldRef: field } = focusOnOpen.current
    const target = first === 'field' ? (field?.current ?? null) : null
    ;(target ?? cancelRef.current)?.focus()
    return () => {
      if (opener !== null && opener.isConnected) opener.focus()
    }
  }, [])

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      // Consumed here, so the panel's document-level Escape does not also close the panel.
      event.preventDefault()
      if (!busy) onCancel()
      return
    }
    if (dialogRef.current !== null) wrapTab(event, dialogRef.current)
  }

  return (
    <div className="library-dialog__scrim">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        aria-describedby={body === undefined ? undefined : bodyId}
        tabIndex={-1}
        className="library-dialog"
        onKeyDown={handleKeyDown}
      >
        <form
          className="library-dialog__form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (!busy) onConfirm()
          }}
        >
          <h2 id={headingId} className="library-dialog__heading">
            {heading}
          </h2>
          {body !== undefined && (
            <p id={bodyId} className="library-dialog__body">
              {body}
            </p>
          )}
          {children}
          {error !== null && (
            <p role="alert" className="library-dialog__error">
              {error}
            </p>
          )}
          <div className="library-dialog__actions">
            <button
              ref={cancelRef}
              type="button"
              className="library-dialog__button"
              aria-disabled={busy || undefined}
              onClick={() => {
                if (!busy) onCancel()
              }}
            >
              {cancelLabel}
            </button>
            <button
              type="submit"
              className={`library-dialog__button ${destructive ? 'library-dialog__button--destructive' : 'library-dialog__button--primary'}`}
              aria-disabled={busy || undefined}
            >
              {confirmLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
