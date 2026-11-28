import * as React from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../ds/Button'
import { LibraryDialog } from '../shell/LibraryDialog'
import { useCanvasState } from '../shell/canvasContext'
import { mintCommandId } from '../shell/tableSessionApi'
import { ALT_MAX_CHARS, AssetRefSchema, IMAGE_MAX_BYTES, MEDIA_TYPES, type AssetRef, type AttachedImageEntry, type ImageUse } from './contracts'
import { attachThreadImage, canUploadPortrait, discardPortraitUpload, uploadPortrait } from './portraitApi'
import './PortraitUpload.css'

interface Props {
  campaignId: string
  getThread: () => Promise<string | null>
  onAttached: (thread: string, entry: AttachedImageEntry) => void
}

export function ThreadImageAttachment({ onOpenChange, triggerRef, ...props }: Props & {
  onOpenChange?: (open: boolean) => void
  triggerRef: React.RefObject<HTMLButtonElement | null>
}) {
  const [open, setOpen] = React.useState(false)
  const closedByUser = React.useRef(false)
  React.useEffect(() => {
    if (!open) return
    onOpenChange?.(true)
    return () => onOpenChange?.(false)
  }, [open, onOpenChange])
  const restoreFocus = React.useCallback(() => {
    if (!closedByUser.current) return
    // A new thread replaces its opener and the workspace removes inert after
    // the dialog unmounts. Keep any focus the GM has already moved elsewhere.
    window.requestAnimationFrame(() => {
      if (document.activeElement === document.body) triggerRef.current?.focus({ preventScroll: true })
    })
  }, [triggerRef])
  return <>
    <Button ref={triggerRef} variant="text" size="small" aria-haspopup="dialog" onClick={() => {
      closedByUser.current = false
      setOpen(true)
    }}>Attach image</Button>
    {open && createPortal(<ImageForm {...props} onClose={() => {
      closedByUser.current = true
      setOpen(false)
    }} restoreFocus={restoreFocus} />, document.body)}
  </>
}

function ImageForm({ campaignId, getThread, onAttached, onClose, restoreFocus }: Props & {
  onClose: () => void
  restoreFocus: () => void
}) {
  const { doc } = useCanvasState()
  const [saved] = React.useState(() => {
    if (doc.kind !== 'open' || doc.document.campaign_id !== campaignId || doc.document.archived) return null
    const ref = AssetRefSchema.safeParse(doc.document.data.portrait)
    return ref.success ? ref.data : null
  })
  const [capability, setCapability] = React.useState<boolean | null>(null)
  const [checks, retryCheck] = React.useReducer(n => n + 1, 0)
  const [source, setSource] = React.useState<'file' | 'document'>('file')
  const [use, setUse] = React.useState<ImageUse>('portrait')
  const [file, setFile] = React.useState<File | null>(null)
  const [alt, setAlt] = React.useState('')
  const [ready, setReady] = React.useState<AssetRef | null>(null)
  const [stage, setStage] = React.useState<'uploading' | 'adding' | 'discarding' | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [discardFailed, setDiscardFailed] = React.useState(false)
  const discardAfter = React.useRef(0)
  const cleanup = React.useRef<AbortController | null>(null)
  const pending = React.useRef<AbortController | null>(null)
  const reservation = React.useRef<string | null>(null)
  const command = React.useRef<string | null>(null)
  const useField = React.useRef<HTMLSelectElement>(null)
  React.useEffect(() => {
    const controller = new AbortController()
    void canUploadPortrait(campaignId, AbortSignal.any([controller.signal, AbortSignal.timeout(8000)])).then(available => {
      if (!controller.signal.aborted) setCapability(available)
    })
    return () => controller.abort()
  }, [campaignId, checks])
  React.useLayoutEffect(() => () => {
    pending.current?.abort()
    if (cleanup.current) cleanup.current.abort()
    else if (reservation.current !== null && Date.now() >= discardAfter.current) {
      void discardPortraitUpload(campaignId, reservation.current)
    }
    reservation.current = null
  }, [campaignId])

  function discard(): void {
    if (reservation.current !== null) void discardPortraitUpload(campaignId, reservation.current)
    reservation.current = null
    command.current = null
    setReady(null)
  }

  async function close(): Promise<void> {
    if (cleanup.current) return
    pending.current?.abort()
    pending.current = null
    const unused = reservation.current
    if (unused === null) { onClose(); return }
    if (Date.now() < discardAfter.current) {
      setDiscardFailed(true)
      setError(discardWaitMessage(discardAfter.current))
      return
    }
    const controller = new AbortController()
    cleanup.current = controller
    setStage('discarding')
    setError(null)
    const result = await discardPortraitUpload(campaignId, unused, fetch, AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]))
    if (controller.signal.aborted) return
    cleanup.current = null
    setStage(null)
    if (result.kind === 'gone') {
      reservation.current = null
      onClose()
      return
    }
    setDiscardFailed(true)
    discardAfter.current = result.after
    setError(result.after > Date.now()
      ? discardWaitMessage(result.after)
      : 'Couldn’t confirm the unused upload was discarded. Retry discard to close.')
  }

  async function add(): Promise<void> {
    if (capability === false) { setCapability(null); retryCheck(); return }
    if (!capability || pending.current || cleanup.current) return
    if (source === 'file' && ready === null) {
      if (!file || !MEDIA_TYPES.image.includes(file.type) || !file.size || file.size > IMAGE_MAX_BYTES) {
        setError('Choose a PNG, JPEG, or WebP file up to 10 MB.')
        return
      }
      if (!alt.trim()) { setError('Enter an image description.'); return }
    }
    const controller = new AbortController()
    pending.current = controller
    const { signal } = controller
    setError(null)
    setDiscardFailed(false)
    try {
      let image = source === 'document' ? saved : ready
      if (image === null && file) {
        setStage('uploading')
        command.current ??= mintCommandId()
        const uploaded = await uploadPortrait(campaignId, file, alt, command.current, signal)
        if (signal.aborted) return
        if (uploaded.kind === 'failed') {
          reservation.current = uploaded.assetId ?? reservation.current
          if (uploaded.startFresh) discard()
          setError(uploaded.message)
          return
        }
        image = uploaded.portrait
        reservation.current = image.asset_id
        setReady(image)
        setFile(null)
      }
      if (!image) return
      setStage('adding')
      const thread = await getThread()
      if (signal.aborted) return
      if (!thread) throw new Error('Couldn’t open a GM thread. Try again with the same image.')
      // Once this POST leaves, its upload is unsafe to discard. Return it to
      // our cleanup only when every attempt came back with a definite refusal.
      const unusedUpload = reservation.current
      reservation.current = null
      const result = await attachThreadImage(campaignId, thread, image.asset_id, use, signal)
      if (result.kind === 'refused' && unusedUpload !== null) {
        if (signal.aborted) void discardPortraitUpload(campaignId, unusedUpload)
        else reservation.current = unusedUpload
      }
      if (signal.aborted) return
      if (result.kind !== 'attached') { setError(result.message); return }
      onAttached(thread, result.entry)
      onClose()
    } catch (cause) {
      if (!signal.aborted) setError(cause instanceof Error ? cause.message : 'Couldn’t add the image. Try again.')
    } finally {
      if (!signal.aborted) {
        pending.current = null
        setStage(null)
      }
    }
  }

  return <LibraryDialog heading="Attach image to GM thread" confirmLabel={
    capability === false ? 'Check again' : capability === null ? 'Checking…' : 'Add to GM thread'
  } cancelLabel={discardFailed ? 'Retry discard' : 'Close'} busy={stage !== null || capability === null}
    allowDismissWhileBusy={stage !== 'discarding'} initialFocus="field" fieldRef={useField}
    error={error} returnFocus={restoreFocus} onConfirm={() => void add()} onCancel={() => void close()}>
    {capability === null && <p role="status">Checking image uploads…</p>}
    {capability === false && <p>Image attachments are unavailable. Check again after image uploads are enabled for this campaign.</p>}
    {capability === true && <>
      <label>Use as
        <select ref={useField} className="gm-field__input" value={use} disabled={stage !== null || ready !== null}
          onChange={event => setUse(event.target.value === 'map' ? 'map' : 'portrait')}>
          <option value="portrait">Portrait</option><option value="map">Map</option>
        </select>
      </label>
      {saved && !ready && <label>
        <input type="checkbox" checked={source === 'document'} disabled={stage !== null} onChange={event => {
          discard()
          setFile(null)
          setAlt('')
          setSource(event.target.checked ? 'document' : 'file')
          setError(null)
        }} /> Use the open document’s image
      </label>}
      {source === 'document' || ready ? <p>{(source === 'document' ? saved : ready)?.alt}</p> : <>
        <label>Image file
          <input type="file" className="portrait-upload__choice" accept={MEDIA_TYPES.image.join(',')} disabled={stage !== null}
            onChange={event => { discard(); setFile(event.target.files?.[0] ?? null); setError(null) }} />
        </label>
        <label>Image description
          <input className="gm-field__input" value={alt} maxLength={ALT_MAX_CHARS} disabled={stage !== null}
            onChange={event => { discard(); setAlt(event.target.value); setError(null) }} />
        </label>
      </>}
      {discardFailed && <button type="button" disabled={stage !== null} onClick={() => {
        reservation.current = null
        onClose()
      }}>Close without discarding</button>}
      {stage && <p role="status">{stage === 'uploading' ? 'Uploading and preparing the image…' :
        stage === 'discarding' ? 'Discarding the unused upload…' : 'Adding to the GM thread…'}</p>}
    </>}
  </LibraryDialog>
}

function discardWaitMessage(after: number): string {
  const time = new Date(after)
  return Number.isFinite(time.getTime())
    ? `Couldn’t discard the unused upload yet. Retry discard after ${time.toLocaleTimeString()}.`
    : 'Couldn’t discard the unused upload yet. Retry after the service’s waiting period.'
}
