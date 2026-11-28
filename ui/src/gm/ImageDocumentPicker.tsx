import * as React from 'react'
import { Button } from '../ds/Button'
import { AssetRefSchema, type AssetRef, type Document, type LibraryItem } from './contracts'
import { getDocument, queryLibrary, saveDocumentField } from './documentApi'
import { REGISTRY, type DocumentType } from './registry'

const types = REGISTRY.document_types.filter(type => type.fields.portrait === 'asset' && type.field_rules.portrait?.editable)

interface Props {
  campaignId: string
  asset: AssetRef
  request: typeof fetch
  /** Historical tool results may describe an older rendition. */
  resolveImage?: (signal: AbortSignal) => Promise<AssetRef | null>
  onConfirmed: (document: Document) => void
  onSaved: () => void
}

export function ImageDocumentPicker(props: Props) {
  const [type, setType] = React.useState(types[0])
  const first = React.useRef<HTMLSelectElement>(null)
  React.useLayoutEffect(() => { first.current?.focus() }, [])
  return <section className="thread-image__picker" aria-label="Link to document">
    <label>Document type
      <select ref={first} value={type.id} onChange={event => {
        const selected = types.find(item => item.id === event.target.value)
        if (selected) setType(selected)
      }}>
        {types.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select>
    </label>
    <DocumentOptions key={type.id} {...props} type={type} />
  </section>
}

function DocumentOptions({ type, ...props }: Props & { type: DocumentType }) {
  const { campaignId, request } = props
  const [rows, setRows] = React.useState<LibraryItem[]>([])
  const [cursor, setCursor] = React.useState<string | null>(null)
  const [page, setPage] = React.useState({ cursor: null as string | null, attempt: 0 })
  const [state, setState] = React.useState<'loading' | 'ready' | 'failed'>('loading')
  const [selected, setSelected] = React.useState('')
  React.useEffect(() => {
    let current = true
    const category = type.library_category
    if (category === 'cues') return
    void queryLibrary({ schema_version: 1, campaign_id: campaignId, category,
      search: '', sort: 'name', archived: false, cursor: page.cursor,
      type: category === 'documents' ? type.id : null }, request).then(result => {
      if (!current) return
      if (result.kind !== 'ok' || result.page.campaign_id !== campaignId || result.page.category !== category) {
        setState('failed')
        return
      }
      const available = result.page.items.filter(row => row.type === type.id && !row.archived)
      setRows(previous => page.cursor === null ? available :
        [...previous, ...available.filter(row => !previous.some(prior => prior.document_id === row.document_id))])
      setCursor(result.page.next_cursor)
      setState('ready')
    })
    return () => { current = false }
  }, [campaignId, type, page, request])
  function loadMore(next: string | null) {
    setState('loading')
    setPage(prior => ({ cursor: next, attempt: prior.attempt + 1 }))
  }
  return <>
    {rows.length > 0 && <label>Document
      <select value={selected} onChange={event => setSelected(event.target.value)}>
        <option value="">Choose a document</option>
        {rows.map(row => <option key={row.document_id} value={row.document_id}>{row.title}{row.qualifier && ` · ${row.qualifier}`}</option>)}
      </select>
    </label>}
    {state === 'loading' && <p role="status">Loading documents…</p>}
    {state === 'failed' && <><p role="status">Couldn’t load documents.</p>
      <Button variant="text" size="small" onClick={() => loadMore(page.cursor)}>Retry documents</Button></>}
    {state === 'ready' && rows.length === 0 && <p>No matching documents.</p>}
    {state === 'ready' && cursor && <Button variant="text" size="small" onClick={() => loadMore(cursor)}>Load more documents</Button>}
    {selected && <PortraitTarget key={selected} {...props} type={type} documentId={selected} />}
  </>
}

function PortraitTarget({ campaignId, asset, request, resolveImage, onConfirmed, onSaved, type, documentId }: Props & { type: DocumentType; documentId: string }) {
  const [document, setDocument] = React.useState<Document | null>(null)
  const [attempt, setAttempt] = React.useState(0)
  const [message, setMessage] = React.useState('')
  const [busy, setBusy] = React.useState(false)
  const active = React.useRef<AbortController | null>(null)
  const pending = React.useRef(false)
  React.useLayoutEffect(() => {
    const controller = new AbortController()
    active.current = controller
    return () => controller.abort()
  }, [])
  React.useEffect(() => {
    let current = true
    void getDocument(campaignId, documentId, request).then(result => {
      if (!current) return
      if (result.kind === 'ok' && result.document.type === type.id && !result.document.archived) setDocument(result.document)
      else setMessage('This document could not be loaded.')
    })
    return () => { current = false }
  }, [campaignId, documentId, type, attempt, request])

  async function link() {
    if (!document || pending.current) return
    pending.current = true
    setBusy(true)
    const signal = active.current!.signal
    const currentImage = resolveImage ? await resolveImage(signal) : asset
    if (signal.aborted) return
    if (!currentImage) {
      pending.current = false
      setBusy(false)
      setMessage('This image is unavailable for linking.')
      return
    }
    const result = await saveDocumentField(document, 'portrait', currentImage, document.write_revision, request)
    if (result.kind === 'ok') {
      const portrait = AssetRefSchema.safeParse(result.document.data.portrait)
      if (portrait.success && portrait.data.asset_id === asset.asset_id) {
        onConfirmed(result.document)
        if (!signal.aborted) onSaved()
        return
      }
    }
    if (signal.aborted) return
    pending.current = false
    setBusy(false)
    setDocument(null)
    setMessage(result.kind === 'conflict' ? 'The document changed. Review its current image before linking.'
      : 'Couldn’t confirm the link. Check the document before trying again.')
  }
  if (!document) return message
    ? <><p role="status">{message}</p><Button variant="text" size="small" onClick={() => {
      setMessage(''); setAttempt(value => value + 1)
    }}>Review current document</Button></>
    : <p role="status">Loading document…</p>
  const previous = AssetRefSchema.safeParse(document.data.portrait)
  if (previous.success && previous.data.asset_id === asset.asset_id) return <p role="status">This image is already linked.</p>
  const replacing = document.data.portrait != null
  return <>
    {message && <p role="status">{message}</p>}
    {replacing && <p>{String(document.data.name)} already has an image. Replace it with this one?</p>}
    <Button variant="filled" aria-disabled={busy || undefined} onClick={() => void link()}>
      {busy ? 'Linking…' : replacing ? 'Replace image' : 'Link image'}
    </Button>
  </>
}
