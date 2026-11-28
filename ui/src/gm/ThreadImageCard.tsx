import * as React from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../ds/Button'
import { DialogTableStop } from '../shell/DialogTableStop'
import { wrapTab } from '../shell/focusTrap'
import { isOpaqueId } from '../shell/workspaceFragment'
import type { AssetRef, DocumentLink, ImageUse } from './contracts'
import { promoteTimelineEntry } from './documentApi'
import { assetPath } from './documentFields'
import { ImageDocumentPicker } from './ImageDocumentPicker'
import { readThreadImage } from './portraitApi'
import './ThreadImageCard.css'

export interface ImageCardScope {
  actorId: string
  conversationId: string
  onSaved: () => void
}

interface Props {
  campaignId: string
  assetId: string
  use: ImageUse
  knownAsset?: AssetRef
  entryId?: string | null
  scope?: ImageCardScope
  onOpen?: (document: DocumentLink) => void
  fetchImpl?: typeof fetch
}

export function ThreadImageCard(props: Props) {
  const { campaignId, assetId, entryId, scope } = props
  if (!isOpaqueId(campaignId) || !isOpaqueId(assetId) || (!props.knownAsset && !scope) ||
      (props.knownAsset && props.knownAsset.asset_id !== assetId)) return <p>This image is unavailable.</p>
  const key = JSON.stringify([scope?.actorId, campaignId, scope?.conversationId, entryId, assetId, props.use])
  return <ImageCard key={key} {...props} />
}

function ImageCard({ campaignId, assetId, use, knownAsset, entryId, scope, onOpen, fetchImpl = fetch }: Props) {
  const [asset, setAsset] = React.useState<AssetRef | null | undefined>(knownAsset)
  const [attempt, setAttempt] = React.useState(0)
  const [viewer, setViewer] = React.useState<'view' | 'link' | null>(null)
  const [message, setMessage] = React.useState('')
  const [saved, setSaved] = React.useState<DocumentLink | null>(null)
  const [saving, setSaving] = React.useState(false)
  const sending = React.useRef(false)
  const opener = React.useRef<HTMLElement | null>(null)
  const description = React.useId()
  const useLabel = React.useId()
  const fallback = React.useRef<HTMLButtonElement>(null)
  const focusRecovery = React.useRef(false)
  const linkButton = React.useRef<HTMLButtonElement>(null)
  const linked = React.useRef(false)
  const life = React.useRef<AbortController | null>(null)
  React.useLayoutEffect(() => {
    const controller = new AbortController()
    life.current = controller
    return () => controller.abort()
  }, [])
  React.useLayoutEffect(() => {
    if (asset === null && focusRecovery.current) {
      fallback.current?.focus()
      focusRecovery.current = false
    }
    if (viewer === 'view' && linked.current) {
      linkButton.current?.focus()
      linked.current = false
    }
  }, [asset, viewer])
  const request = React.useCallback<typeof fetch>(async (path, init) => {
    const signal = life.current!.signal
    signal.throwIfAborted()
    const response = await fetchImpl(path, { ...init, mode: 'same-origin', redirect: 'error', signal })
    signal.throwIfAborted()
    return response
  }, [fetchImpl])
  const path = assetPath(campaignId, assetId)
  const label = use === 'map' ? 'Map' : 'Portrait'
  const actions = scope !== undefined && Boolean(entryId)

  React.useEffect(() => {
    if (knownAsset && attempt === 0) return
    const controller = new AbortController()
    void readThreadImage(campaignId, assetId, controller.signal, fetchImpl).then(ref => {
      if (!controller.signal.aborted) setAsset(ref)
    })
    return () => controller.abort()
  }, [knownAsset, attempt, fetchImpl, campaignId, assetId])

  function open(trigger: HTMLElement, mode: 'view' | 'link') {
    opener.current = trigger
    setMessage('')
    setViewer(mode)
  }
  function close() {
    setViewer(null)
  }
  const restoreFocus = React.useCallback(() => {
    const target = opener.current?.isConnected ? opener.current : fallback.current
    target?.focus()
  }, [])

  async function save() {
    if (!entryId || !scope || sending.current) return
    if (saved) {
      close()
      onOpen?.(saved)
      return
    }
    const signal = life.current!.signal
    sending.current = true
    setSaving(true)
    setMessage('')
    try {
      const result = await promoteTimelineEntry(campaignId, scope.conversationId, entryId, 'handout', request)
      if (signal.aborted) return
      if (result.kind === 'ok' && !result.document.archived) {
        setSaved({ document_id: result.document.document_id, type: 'handout', library_category: 'documents',
          title: String(result.document.data.name) })
        setMessage('Saved as a handout.')
        scope.onSaved()
      } else if (result.kind === 'limit') setMessage('The account’s document storage is full.')
      else if (result.kind === 'throttled') setMessage('Please wait a moment before trying again.')
      else if (result.kind === 'unavailable') setMessage('This image is unavailable.')
      else setMessage('Couldn’t save this handout. Try again.')
    } finally {
      sending.current = false
      if (!signal.aborted) setSaving(false)
    }
  }

  function commands(inViewer: boolean) {
    if (!actions) return null
    return <div className="thread-image__actions">
      <Button variant="outlined" size="small" aria-disabled={saving || (saved !== null && !onOpen) || undefined}
        onClick={() => void save()}>{saved ? (onOpen ? 'Open handout' : 'Handout saved') : saving ? 'Saving…' : 'Save as handout'}</Button>
      <Button ref={inViewer ? linkButton : undefined} variant="outlined" size="small" onClick={event => {
        if (inViewer) { setMessage(''); setViewer('link') }
        else open(event.currentTarget, 'link')
      }}>Link to document</Button>
    </div>
  }

  if (!asset) return <div className="thread-image">
    <p>{asset === undefined ? 'Loading image…' : 'This image is unavailable.'}</p>
    {asset === null && <Button ref={fallback} variant="text" size="small" onClick={() => {
      setAsset(undefined); setAttempt(value => value + 1)
    }}>Retry image</Button>}
  </div>

  function unavailable(event: React.SyntheticEvent<HTMLImageElement>) {
    const card = event.currentTarget.closest('.thread-image')
    focusRecovery.current = viewer !== null || card?.contains(document.activeElement) === true
    setViewer(null)
    setAsset(null)
    setSaved(null)
    setMessage('')
  }
  return <div className="thread-image">
    <strong id={useLabel} className="thread-image__use">{label}</strong>
    <button type="button" className="thread-image__preview" aria-label="Expand image" aria-describedby={`${useLabel} ${description}`} aria-haspopup="dialog"
      onClick={event => open(event.currentTarget, 'view')}>
      <img id={description} src={assetPath(campaignId, assetId, 'thumb')} alt={asset.alt} onError={unavailable} />
      <span>Expand image</span>
    </button>
    {commands(false)}
    {message && !viewer && <p role="status">{message}</p>}
    {viewer && <ImageViewer label={label} close={close} restoreFocus={restoreFocus}>
      <img className="thread-image__full" src={path} alt={asset.alt} onError={unavailable} />
      {viewer === 'link' && scope
        ? <ImageDocumentPicker campaignId={campaignId} asset={asset} request={request} onSaved={() => {
          scope.onSaved()
          setMessage('Image linked to document.')
          linked.current = true
          setViewer('view')
        }} />
        : commands(true)}
      {message && <p role="status">{message}</p>}
    </ImageViewer>}
  </div>
}

function ImageViewer({ label, close, restoreFocus, children }: {
  label: string; close: () => void; restoreFocus: () => void; children: React.ReactNode
}) {
  const title = React.useId()
  const dialog = React.useRef<HTMLDivElement>(null)
  React.useLayoutEffect(() => {
    const background = document.getElementById('root')
    const wasInert = background?.inert
    if (background) background.inert = true
    if (!dialog.current?.contains(document.activeElement)) dialog.current?.focus()
    return () => {
      if (background) background.inert = wasInert ?? false
      restoreFocus()
    }
  }, [restoreFocus])
  return createPortal(<div className="thread-image__backdrop">
    <div role="dialog" aria-modal="true" aria-labelledby={title} tabIndex={-1}
      className="thread-image__viewer" ref={dialog} onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
        else wrapTab(event, event.currentTarget)
      }}>
      <header><h2 id={title} tabIndex={-1}>{label}</h2>
        <Button variant="text" size="small" onClick={close}>Close</Button></header>
      <DialogTableStop />
      {children}
    </div>
  </div>, document.body)
}
