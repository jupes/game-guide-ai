/**
 * LibraryDialogs -- the three dialogs the Campaign Library raises
 * (agent-forge-harness-1kg.6.4, PR-2; LIB-12, LIB-17, LIB-18, SEC-40), each a thin
 * layer of copy and rules over `LibraryDialog`'s modal frame.
 *
 * - `LiveArchiveDialog`: the one archive that asks first, because players are affected
 *   (LIB-17). Initial focus is Cancel, the safe choice.
 * - `DeleteDocumentDialog`: names the document and asks for the password again (LIB-18,
 *   SEC-40). It makes the request itself so the password never leaves this component's
 *   state: it is not lifted, logged, stored, echoed or put in a URL, it is cleared after
 *   a refusal, and it unmounts with the dialog. Initial focus is the password field.
 * - `StatBlockDialog`: a stat block without a name, armor class and hit points is invalid
 *   (LIB-12), so nothing is sent until all three are valid; each refusal sits under its
 *   own field and the first one takes focus.
 *
 * Titles and field text are GM-private (X-7): shown, never kept anywhere else.
 */

import * as React from 'react'
import { codePointLength, INTEGER_FIELD_BOUNDS, PASSWORD_MAX_CHARS, TEXT_FIELD_MAX_CHARS, trimWire } from '../gm/contracts'
import { deleteDocument } from '../gm/documentApi'
import { TextField } from '../ds/TextField'
import { LibraryDialog } from './LibraryDialog'
import { LIBRARY_COPY } from './workbenchCopy'

// ── LIB-17 ───────────────────────────────────────────────────────────────────

export function LiveArchiveDialog({
  title, onConfirm, onCancel,
}: { title: string; onConfirm: () => void; onCancel: () => void }): React.JSX.Element {
  return (
    <LibraryDialog
      heading={LIBRARY_COPY.liveArchiveHeading(title)}
      body={LIBRARY_COPY.liveArchiveBody}
      confirmLabel={LIBRARY_COPY.archive}
      cancelLabel={LIBRARY_COPY.cancel}
      busy={false}
      initialFocus="cancel"
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  )
}

// ── LIB-18, SEC-40 ───────────────────────────────────────────────────────────

/** How a delete ended, for the panel: the dialog handles every refusal that leaves it open. */
export type DeleteOutcome = 'deleted' | 'gone' | 'not_archived' | 'unauthorized'

export interface DeleteDocumentDialogProps {
  campaignId: string
  documentId: string
  title: string
  fetchImpl?: typeof fetch
  onCancel: () => void
  onDone: (outcome: DeleteOutcome) => void
}

export function DeleteDocumentDialog({
  campaignId, documentId, title, fetchImpl, onCancel, onDone,
}: DeleteDocumentDialogProps): React.JSX.Element {
  const [password, setPassword] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const fieldRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)

  React.useLayoutEffect(() => {
    const input = fieldRef.current
    if (input instanceof HTMLInputElement) {
      input.maxLength = PASSWORD_MAX_CHARS
      input.autocomplete = 'current-password'
    }
  }, [])

  const submit = async (): Promise<void> => {
    if (busy) return
    if (password === '') {
      setError(LIBRARY_COPY.passwordNeeded)
      fieldRef.current?.focus()
      return
    }
    setBusy(true)
    setError(null)
    const result = await deleteDocument(campaignId, documentId, password, fetchImpl)
    setBusy(false)
    switch (result.kind) {
      case 'ok':
        return onDone('deleted')
      case 'unavailable':
        return onDone('gone')
      case 'not_archived':
        return onDone('not_archived')
      case 'unauthorized':
        return onDone('unauthorized')
      case 'reauth_failed':
        setPassword('')
        setError(LIBRARY_COPY.passwordWrong)
        break
      case 'throttled':
        setError(LIBRARY_COPY.throttled)
        break
      case 'failed':
        setError(LIBRARY_COPY.deleteFailed(title))
        break
    }
    fieldRef.current?.focus()
  }

  return (
    <LibraryDialog
      heading={LIBRARY_COPY.deleteHeading(title)}
      body={LIBRARY_COPY.deleteBody(title)}
      confirmLabel={busy ? LIBRARY_COPY.deleting : LIBRARY_COPY.delete}
      cancelLabel={LIBRARY_COPY.cancel}
      busy={busy}
      destructive
      initialFocus="field"
      fieldRef={fieldRef}
      error={error}
      onConfirm={() => void submit()}
      onCancel={onCancel}
    >
      <TextField
        label={LIBRARY_COPY.password}
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        fullWidth
        ref={fieldRef}
      />
    </LibraryDialog>
  )
}

// ── LIB-12: the stat block ───────────────────────────────────────────────────

export interface StatBlockValues {
  readonly name: string
  readonly fields: { readonly ac: number; readonly hp: number }
}

export interface StatBlockDialogProps {
  busy: boolean
  /** The server's or contract's refusal of the last submit, shown in the dialog. */
  error: string | null
  onSubmit: (values: StatBlockValues) => void
  onCancel: () => void
}

const [AC_MIN, AC_MAX] = INTEGER_FIELD_BOUNDS.statblock.ac
const [HP_MIN, HP_MAX] = INTEGER_FIELD_BOUNDS.statblock.hp

/** A whole number inside `[min, max]`, written in digits only, or null. */
function wholeNumber(raw: string, min: number, max: number): number | null {
  const text = raw.trim()
  if (!/^\d{1,9}$/.test(text)) return null
  const value = Number(text)
  return value >= min && value <= max ? value : null
}

interface Problems {
  readonly name?: string
  readonly ac?: string
  readonly hp?: string
}

export function StatBlockDialog({ busy, error, onSubmit, onCancel }: StatBlockDialogProps): React.JSX.Element {
  const [name, setName] = React.useState('')
  const [ac, setAc] = React.useState('')
  const [hp, setHp] = React.useState('')
  const [problems, setProblems] = React.useState<Problems>({})
  const nameRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const acRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const hpRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const ids = React.useId()
  const problemId = (field: keyof Problems): string => `${ids}-${field}-problem`

  React.useLayoutEffect(() => {
    if (nameRef.current instanceof HTMLInputElement) nameRef.current.maxLength = TEXT_FIELD_MAX_CHARS
    for (const ref of [acRef, hpRef]) if (ref.current instanceof HTMLInputElement) ref.current.inputMode = 'numeric'
  }, [])

  const submit = (): void => {
    if (busy) return
    const trimmed = trimWire(name)
    const acValue = wholeNumber(ac, AC_MIN, AC_MAX)
    const hpValue = wholeNumber(hp, HP_MIN, HP_MAX)
    const found: { name?: string; ac?: string; hp?: string } = {}
    if (trimmed === '' || codePointLength(trimmed) > TEXT_FIELD_MAX_CHARS) found.name = LIBRARY_COPY.statNameRequired
    if (acValue === null) found.ac = LIBRARY_COPY.statNumberInvalid(LIBRARY_COPY.statAc, AC_MAX)
    if (hpValue === null) found.hp = LIBRARY_COPY.statNumberInvalid(LIBRARY_COPY.statHp, HP_MAX)
    setProblems(found)
    if (found.name !== undefined) nameRef.current?.focus()
    else if (found.ac !== undefined) acRef.current?.focus()
    else if (found.hp !== undefined) hpRef.current?.focus()
    if (acValue === null || hpValue === null || found.name !== undefined) return
    onSubmit({ name: trimmed, fields: { ac: acValue, hp: hpValue } })
  }

  const field = (
    key: keyof Problems, label: string, value: string, set: (value: string) => void,
    ref: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>,
  ): React.JSX.Element => {
    const problem = problems[key]
    return (
      <div>
        <TextField
          label={label}
          value={value}
          onChange={(event) => set(event.target.value)}
          fullWidth
          error={problem !== undefined}
          aria-invalid={problem !== undefined || undefined}
          aria-describedby={problem === undefined ? undefined : problemId(key)}
          ref={ref}
        />
        {problem !== undefined && (
          <p id={problemId(key)} className="library-dialog__field-error">
            {problem}
          </p>
        )}
      </div>
    )
  }

  return (
    <LibraryDialog
      heading={LIBRARY_COPY.statBlockHeading}
      body={LIBRARY_COPY.statBlockBody}
      confirmLabel={busy ? LIBRARY_COPY.creating : LIBRARY_COPY.statCreate}
      cancelLabel={LIBRARY_COPY.cancel}
      busy={busy}
      initialFocus="field"
      fieldRef={nameRef}
      error={error}
      onConfirm={submit}
      onCancel={onCancel}
    >
      <div className="library-dialog__fields">
        {field('name', LIBRARY_COPY.statName, name, setName, nameRef)}
        {field('ac', LIBRARY_COPY.statAc, ac, setAc, acRef)}
        {field('hp', LIBRARY_COPY.statHp, hp, setHp, hpRef)}
      </div>
    </LibraryDialog>
  )
}
