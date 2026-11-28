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
  onOpenChange?: (open: boolean) => void
}

export function ThreadImageAttachment({ onOpenChange, ...props }: Props) {
  const [open, setOpen] = React.useState(false)
  const trigger = React.useRef<HTMLButtonElement>(null)
  React.useEffect(() => {
    if (!open) return
    onOpenChange?.(true)
    return () => onOpenChange?.(false)
  }, [open, onOpenChange])
  const restoreFocus = React.useCallback(() => { trigger.current?.focus({ preventScroll: true }) }, [])
  return <>
    <Button ref={trigger} variant="text" size="small" aria-haspopup="dialog" onClick={() => setOpen(true)}>Attach image</Button>
    {open && createPortal(<ImageForm {...props} onClose={() => setOpen(false)} restoreFocus={restoreFocus} />, document.body)}
  </>
}

function ImageForm({ campaignId, getThread, onAttached, onClose, restoreFocus }: Omit<Props, 'onOpenChange'> & {
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
  const [stage, setStage] = React.useState<'uploading' | 'adding' | null>(null)
  const [error, setError] = React.useState<string | null>(null)
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
    if (reservation.current !== null) void discardPortraitUpload(campaignId, reservation.current)
    reservation.current = null
  }, [campaignId])

  function discard(): void {
    if (reservation.current !== null) void discardPortraitUpload(campaignId, reservation.current)
    reservation.current = null
    command.current = null
    setReady(null)
  }

  async function add(): Promise<void> {
    if (capability === false) { setCapability(null); retryCheck(); return }
    if (!capability || pending.current) return
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
      // A lost acknowledgement can still mean the attachment was saved.
      // Never delete its asset after sending this idempotent write.
      reservation.current = null
      const entry = await attachThreadImage(campaignId, thread, image.asset_id, use, signal)
      if (!signal.aborted) {
        onAttached(thread, entry)
        onClose()
      }
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
  } cancelLabel="Close" busy={stage !== null || capability === null} allowDismissWhileBusy initialFocus="field" fieldRef={useField}
    error={error} returnFocus={restoreFocus} onConfirm={() => void add()} onCancel={() => {
      pending.current?.abort()
      discard()
      onClose()
    }}>
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
      {stage && <p role="status">{stage === 'uploading' ? 'Uploading and preparing the image…' : 'Adding to the GM thread…'}</p>}
    </>}
  </LibraryDialog>
}
