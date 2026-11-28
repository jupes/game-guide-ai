import * as React from 'react'
import { Button } from '../ds/Button'
import { mintCommandId } from '../shell/tableSessionApi'
import { ALT_MAX_CHARS, IMAGE_MAX_BYTES, MEDIA_TYPES, type AssetRef } from './contracts'
import { canUploadPortrait, discardDetachedPortraitUpload, discardPortraitUpload, uploadPortrait } from './portraitApi'
import './PortraitUpload.css'

interface PortraitUploadProps {
  campaignId: string
  hasPortrait: boolean
  locked: boolean
  fetchImpl?: typeof fetch
  onStart: () => void
  onCancel: () => void
  onAttach: (portrait: AssetRef) => void
}

export function PortraitUpload({
  campaignId, hasPortrait, locked, fetchImpl, onStart, onCancel, onAttach,
}: PortraitUploadProps): React.JSX.Element | null {
  const [available, setAvailable] = React.useState(false)
  const [open, setOpen] = React.useState(false)
  const [file, setFile] = React.useState<File | null>(null)
  const [alt, setAlt] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState('')
  const fileRef = React.useRef<HTMLInputElement>(null)
  const triggerRef = React.useRef<HTMLButtonElement>(null)
  const pending = React.useRef<AbortController | null>(null)
  const reserved = React.useRef<string | null>(null)
  const command = React.useRef<string | null>(null)
  const id = React.useId()

  React.useEffect(() => {
    const capability = new AbortController()
    void canUploadPortrait(campaignId, capability.signal, fetchImpl).then((enabled) => {
      if (!capability.signal.aborted) setAvailable(enabled)
    })
    return () => {
      capability.abort()
      pending.current?.abort()
      const unused = reserved.current
      if (unused !== null) void discardDetachedPortraitUpload(campaignId, unused, fetchImpl)
    }
  }, [campaignId, fetchImpl])

  React.useEffect(() => {
    if (open) fileRef.current?.focus({ preventScroll: true })
  }, [open])

  function close(): void {
    setOpen(false)
    setBusy(false)
    setError('')
    setFile(null)
    setAlt('')
    command.current = null
    triggerRef.current?.focus({ preventScroll: true })
  }

  function cancel(): void {
    const sending = pending.current
    if (sending !== null) {
      sending.abort()
      pending.current = null
      onCancel()
    }
    discardReservation()
    close()
  }

  function discardReservation(): void {
    const unused = reserved.current
    reserved.current = null
    command.current = null
    if (unused !== null) void discardPortraitUpload(campaignId, unused, fetchImpl)
  }

  async function submit(event: React.FormEvent): Promise<void> {
    event.preventDefault()
    if (pending.current !== null || file === null) return
    if (!MEDIA_TYPES.image.includes(file.type) || file.size === 0 || file.size > IMAGE_MAX_BYTES) {
      setError('Choose a PNG, JPEG, or WebP file up to 10 MB.')
      return
    }
    if (alt.trim() === '') {
      setError('Enter an image description.')
      return
    }
    const sending = new AbortController()
    pending.current = sending
    command.current ??= mintCommandId()
    setBusy(true)
    setError('')
    onStart()
    const result = await uploadPortrait(campaignId, file, alt, command.current, sending.signal, fetchImpl)
    if (sending.signal.aborted) return
    pending.current = null
    setBusy(false)
    if (result.kind === 'ready') {
      reserved.current = null
      onAttach(result.portrait)
      close()
    } else {
      onCancel()
      reserved.current = result.assetId ?? reserved.current
      if (result.startFresh === true) discardReservation()
      setError(result.message)
    }
  }

  if (!available) return null
  return (
    <div className="portrait-upload">
      <Button
        ref={triggerRef}
        variant="outlined"
        size="small"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-disabled={!open && locked}
        onClick={() => {
          if (open) cancel()
          else if (!locked) setOpen(true)
        }}
      >
        {hasPortrait ? 'Replace portrait' : 'Attach portrait'}
      </Button>
      {open && (
        <form id={id} className="portrait-upload__form" onSubmit={(event) => void submit(event)} aria-label="Portrait upload">
          <label htmlFor={`${id}-image`}>Image file</label>
          <input
            className="portrait-upload__choice"
            ref={fileRef}
            id={`${id}-image`}
            type="file"
            accept={MEDIA_TYPES.image.join(',')}
            disabled={busy}
            required
            onChange={(event) => {
              discardReservation()
              setFile(event.target.files?.[0] ?? null)
              setError('')
            }}
          />
          <label htmlFor={`${id}-alt`}>Image description</label>
          <input
            className="gm-field__input"
            id={`${id}-alt`}
            value={alt}
            maxLength={ALT_MAX_CHARS}
            required
            disabled={busy}
            onChange={(event) => {
              discardReservation()
              setAlt(event.target.value)
              setError('')
            }}
          />
          {busy && <p role="status">Uploading and preparing the portrait…</p>}
          {error !== '' && <p className="gm-field__problem" role="alert">{error}</p>}
          <div className="portrait-upload__controls">
            <Button type="submit" variant="filled" size="small" disabled={busy}>Upload portrait</Button>
            <Button variant="text" size="small" onClick={cancel}>Cancel upload</Button>
          </div>
        </form>
      )}
    </div>
  )
}
