import * as React from 'react'

import { Button } from '../ds/Button'
import { TextField } from '../ds/TextField'
import { CuePreview } from '../gm/CuePreview'
import {
  CONTRACT_VERSION, CUE_TITLE_MAX_CHARS, CueListQuerySchema, SEARCH_MAX_CHARS, SEARCH_MIN_CHARS, codePointLength, trimWire,
  type Cue, type CueKind, type CuePage,
} from '../gm/contracts'
import { CueApiError, LiveCueArchiveError, deleteCue, discardCueUpload, listCues, updateCue, uploadCue, type CueUploadIntent } from '../gm/cueApi'
import { LibraryDialog } from './LibraryDialog'
import { PasswordDeleteDialog, type DeleteOutcome } from './LibraryDialogs'
import { mintCommandId } from './tableSessionApi'
import { LIBRARY_PAGE_SIZE, SEARCH_DEBOUNCE_MS } from './useLibraryList'
import { LIBRARY_COPY } from './workbenchCopy'
import './CueLibrary.css'

function UploadCueForm({
  campaign, fetchImpl, onSaved, onCancel,
}: {
  campaign: string
  fetchImpl: typeof fetch
  onSaved: (cue: Cue) => void
  onCancel: () => void
}): React.JSX.Element {
  const [title, setTitle] = React.useState('')
  const [kind, setKind] = React.useState<CueKind>('ambience')
  const [file, setFile] = React.useState<File | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [started, setStarted] = React.useState(false)
  const [error, setError] = React.useState('')
  const intent = React.useRef<CueUploadIntent | null>(null)
  const pending = React.useRef<AbortController | null>(null)
  const titleField = React.useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    titleField.current?.focus()
    return () => {
      const running = pending.current
      if (running !== null) running.abort()
      else if (intent.current !== null) void discardCueUpload(campaign, intent.current, fetchImpl)
    }
  }, [campaign, fetchImpl])

  async function submit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (file === null || pending.current !== null) return
    const attempt = intent.current ?? { commandId: mintCommandId() }
    intent.current = attempt
    const abort = new AbortController()
    pending.current = abort
    setBusy(true)
    setError('')
    try {
      const saved = await uploadCue(campaign, title, kind, file, attempt, abort.signal, fetchImpl)
      if (abort.signal.aborted) return
      intent.current = null
      onSaved(saved)
    } catch (reason) {
      if (!abort.signal.aborted) setError(reason instanceof CueApiError ? reason.message : "Couldn't upload the sound.")
    } finally {
      pending.current = null
      if (abort.signal.aborted) void discardCueUpload(campaign, attempt, fetchImpl)
      else {
        setStarted(attempt.assetId !== undefined)
        setBusy(false)
      }
    }
  }

  return (
    <form className="cue-library__form" aria-label="Upload sound" onSubmit={(event) => void submit(event)}>
      <TextField ref={titleField} label="Cue title" value={title} maxLength={CUE_TITLE_MAX_CHARS}
        disabled={busy || started} onChange={(event) => setTitle(event.target.value)} fullWidth />
      <label className="cue-library__field">
        Kind
        <select value={kind} disabled={busy || started} onChange={(event) => {
          setKind(event.target.value === 'one_shot' ? 'one_shot' : 'ambience')
        }}>
          <option value="ambience">Ambience</option>
          <option value="one_shot">One-shot</option>
        </select>
      </label>
      <label className="cue-library__field">
        Sound file
        <input type="file" accept=".mp3,.m4a,.ogg,.wav,audio/mpeg,audio/mp4,audio/ogg,audio/wav"
          disabled={busy || started} onChange={(event) => {
            if (intent.current?.assetId === undefined) intent.current = null
            setFile(event.target.files?.[0] ?? null)
          }} />
      </label>
      {error && <p role="alert">{error}</p>}
      <div className="cue-library__actions">
        <Button type="submit" disabled={busy || file === null || trimWire(title) === ''}>
          {busy ? 'Uploading…' : started ? 'Try again' : 'Upload'}
        </Button>
        <Button variant="text" onClick={onCancel}>Cancel upload</Button>
      </div>
    </form>
  )
}

interface CueLibraryProps {
  campaign: string
  fetchImpl?: typeof fetch
  announce: (message: string) => void
  onAttach?: (cue: Cue) => Promise<void>
}

export function CueLibrary({ campaign, fetchImpl = fetch, announce, onAttach }: CueLibraryProps): React.JSX.Element {
  const [search, setSearch] = React.useState('')
  const [sort, setSort] = React.useState<'recent' | 'name'>('recent')
  const [archived, setArchived] = React.useState(false)
  const [revision, refresh] = React.useReducer((value: number) => value + 1, 0)
  const [loaded, setLoaded] = React.useState<{ key: string; page: CuePage | null } | null>(null)
  const [lastMoreError, setLastMoreError] = React.useState<string | null>(null)
  const [loadingMore, setLoadingMore] = React.useState(false)
  const [expanded, setExpanded] = React.useState<string | null>(null)
  const [uploading, setUploading] = React.useState(false)
  const [titleEdit, setTitleEdit] = React.useState<string | null>(null)
  const [saving, setSaving] = React.useState(false)
  const [editError, setEditError] = React.useState('')
  const [undoCue, setUndoCue] = React.useState<Cue | null>(null)
  const [deleteTarget, setDeleteTarget] = React.useState<Cue | null>(null)
  const [liveArchive, setLiveArchive] = React.useState<Cue | null>(null)
  const requestScope = React.useRef<AbortController | null>(null)
  const panelRef = React.useRef<HTMLDivElement>(null)
  const uploadButton = React.useRef<HTMLButtonElement>(null)
  const searchField = React.useRef<HTMLInputElement>(null)
  const renameField = React.useCallback((node: HTMLInputElement | null) => node?.focus(), [])
  const searchText = trimWire(search)
  const query = React.useMemo(() => CueListQuerySchema.safeParse({
    schema_version: CONTRACT_VERSION, campaign_id: campaign,
    search: codePointLength(searchText) < SEARCH_MIN_CHARS ? '' : searchText,
    archived, sort, limit: LIBRARY_PAGE_SIZE,
  }), [archived, campaign, searchText, sort])
  const key = JSON.stringify([campaign, query.success ? query.data : null, revision])
  const active = loaded?.key === key ? loaded : null

  React.useEffect(() => {
    if (undoCue === null) return
    const expires = setTimeout(() => setUndoCue(null), 8000)
    return () => clearTimeout(expires)
  }, [undoCue])

  React.useEffect(() => {
    if (!query.success) return
    const abort = new AbortController()
    requestScope.current = abort
    const timer = setTimeout(() => {
      void listCues(query.data, abort.signal, fetchImpl).then(
        (page) => { if (!abort.signal.aborted) setLoaded({ key, page }) },
        () => { if (!abort.signal.aborted) setLoaded({ key, page: null }) },
      )
    }, query.data.search === '' ? 0 : SEARCH_DEBOUNCE_MS)
    return () => { clearTimeout(timer); abort.abort() }
  }, [fetchImpl, key, query])

  async function more(): Promise<void> {
    const page = active?.page
    const abort = requestScope.current
    if (!query.success || abort === null || page?.next_cursor == null || loadingMore) return
    setLoadingMore(true)
    setLastMoreError(null)
    try {
      const next = await listCues({ ...query.data, cursor: page.next_cursor }, abort.signal, fetchImpl)
      if (abort.signal.aborted) return
      const seen = new Set(page.items.map((item) => item.cue_id))
      setLoaded({ key, page: { ...next, items: [...page.items, ...next.items.filter((item) => !seen.has(item.cue_id))] } })
    } catch {
      if (!abort.signal.aborted) setLastMoreError(key)
    } finally {
      setLoadingMore(false)
    }
  }

  async function change(cue: Cue, action: { title: string } | 'archive' | 'restore', confirmStop = false): Promise<void> {
    if (saving) return
    setSaving(true)
    setEditError('')
    try {
      const saved = await updateCue(cue, action, fetchImpl, confirmStop)
      setLiveArchive(null)
      if (typeof action === 'string') setExpanded(null)
      if (action === 'archive') setUndoCue(saved)
      if (action === 'restore') setUndoCue((previous) => previous?.cue_id === saved.cue_id ? null : previous)
      panelRef.current?.focus()
      setTitleEdit(null)
      refresh()
      announce(typeof action === 'string' ? `${saved.title} ${action === 'archive' ? 'archived' : 'restored'}` : 'Cue renamed')
    } catch (reason) {
      if (reason instanceof LiveCueArchiveError) setLiveArchive(cue)
      else setEditError(reason instanceof CueApiError ? reason.message : "Couldn't save the change.")
    } finally {
      setSaving(false)
    }
  }

  function endUpload(): void {
    setUploading(false)
    // The upload button stays mounted while the form is open.
    uploadButton.current?.focus()
  }

  function finishDelete(cue: Cue, outcome: DeleteOutcome): void {
    setDeleteTarget(null)
    setUndoCue((previous) => previous?.cue_id === cue.cue_id ? null : previous)
    setExpanded(null)
    refresh()
    panelRef.current?.focus()
    if (outcome === 'deleted') announce(LIBRARY_COPY.deleted(cue.title))
    if (outcome === 'not_archived') announce(LIBRARY_COPY.notArchived(cue.title))
    if (outcome === 'gone') announce('Cue unavailable.')
  }

  async function addToThread(cue: Cue): Promise<void> {
    if (saving || onAttach === undefined) return
    setSaving(true)
    setEditError('')
    try {
      await onAttach(cue)
    } catch (reason) {
      setEditError(reason instanceof CueApiError ? reason.message : "Couldn't add the cue to the GM thread.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="cue-library" ref={panelRef} tabIndex={-1}>
      <div className="cue-library__actions">
        <Button ref={uploadButton} aria-expanded={uploading} onClick={() => setUploading(true)} icon="upload">
          Upload sound
        </Button>
      </div>
      {uploading && <UploadCueForm campaign={campaign} fetchImpl={fetchImpl} onCancel={endUpload} onSaved={(cue) => {
        endUpload()
        setArchived(false)
        setSearch('')
        setExpanded(cue.cue_id)
        refresh()
        announce(`Uploaded ${cue.title}`)
      }} />}
      <TextField ref={searchField} label="Search cues" value={search} maxLength={SEARCH_MAX_CHARS}
        onChange={(event) => setSearch(event.target.value)} fullWidth />
      <div className="cue-library__filters">
        <label className="cue-library__field">
          Sort
          <select value={sort} onChange={(event) => setSort(event.target.value === 'name' ? 'name' : 'recent')}>
            <option value="recent">{LIBRARY_COPY.recent}</option>
            <option value="name">{LIBRARY_COPY.name}</option>
          </select>
        </label>
        <label className="cue-library__field">
          Show
          <select value={archived ? 'archived' : 'active'} onChange={(event) => setArchived(event.target.value === 'archived')}>
            <option value="active">Active</option>
            <option value="archived">Archived</option>
          </select>
        </label>
      </div>
      {undoCue && (
        <div className="cue-library__actions">
          <span>{undoCue.title} archived</span>
          <Button variant="text" disabled={saving} onClick={() => void change(undoCue, 'restore')}>Undo</Button>
        </div>
      )}
      {liveArchive && <LibraryDialog heading={`Archive ${liveArchive.title}?`}
        body="This cue is playing at the table. Archiving it will stop playback for everyone."
        confirmLabel="Stop and archive" cancelLabel="Cancel" initialFocus="cancel" busy={saving}
        onConfirm={() => void change(liveArchive, 'archive', true)} onCancel={() => { setLiveArchive(null); setEditError('') }}>
        {editError && <p role="alert">{editError}</p>}
      </LibraryDialog>}
      {!liveArchive && editError && <p role="alert">{editError}</p>}
      {!query.success ? <p>{LIBRARY_COPY.searchInvalid}</p> : active === null ? <p>Loading cues…</p> : active.page === null ? (
        <p>Couldn't load cues. <Button variant="text" onClick={refresh}>Retry</Button></p>
      ) : active.page.items.length === 0 ? (
        <p>
          {query.data.search ? LIBRARY_COPY.noMatch(query.data.search) : archived ? 'No archived cues.' : 'Upload a sound to get started.'}
          {query.data.search && <> <Button variant="text" onClick={() => {
            setSearch('')
            searchField.current?.focus()
          }}>Clear search</Button></>}
        </p>
      ) : (
        <ul className="cue-library__list" aria-label="Cues">
          {active.page.items.map((cue) => (
            <li key={cue.cue_id}>
              <Button variant="tonal" fullWidth aria-expanded={expanded === cue.cue_id} onClick={() => {
                setExpanded(expanded === cue.cue_id ? null : cue.cue_id)
                setTitleEdit(null)
                setEditError('')
              }}>{cue.title}</Button>
              {expanded === cue.cue_id && (
                <>
                  {deleteTarget === null && <CuePreview cue={cue} fetchImpl={fetchImpl} />}
                  {titleEdit === null ? (
                    <div className="cue-library__actions">
                      {!cue.archived && onAttach && (
                        <Button variant="text" disabled={saving} onClick={() => void addToThread(cue)}>Add to thread</Button>
                      )}
                      {!cue.archived && <Button variant="text" onClick={() => setTitleEdit(cue.title)}>Rename</Button>}
                      <Button variant="text" disabled={saving} onClick={() => void change(cue, cue.archived ? 'restore' : 'archive')}>
                        {cue.archived ? 'Restore' : 'Archive'}
                      </Button>
                      {cue.archived && (
                        <Button variant="text" disabled={saving} aria-label={LIBRARY_COPY.deleteNamed(cue.title)}
                          onClick={() => setDeleteTarget(cue)}>
                          {LIBRARY_COPY.delete}
                        </Button>
                      )}
                    </div>
                  ) : (
                    <form className="cue-library__form" aria-label="Rename cue" onSubmit={(event) => {
                      event.preventDefault()
                      void change(cue, { title: titleEdit })
                    }}>
                      <TextField ref={renameField} label="Cue title" value={titleEdit} maxLength={CUE_TITLE_MAX_CHARS}
                        onChange={(event) => setTitleEdit(event.target.value)} fullWidth />
                      <div className="cue-library__actions">
                        <Button type="submit" disabled={saving || trimWire(titleEdit) === ''}>Save title</Button>
                        <Button variant="text" onClick={() => {
                          setTitleEdit(null)
                          panelRef.current?.focus()
                        }}>Cancel rename</Button>
                      </div>
                    </form>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {active?.page?.next_cursor && (
        <Button variant="text" disabled={loadingMore} onClick={() => void more()}>
          {loadingMore ? 'Loading…' : lastMoreError === key ? 'Retry load more' : 'Load more'}
        </Button>
      )}
      {deleteTarget && (
        <PasswordDeleteDialog
          title={deleteTarget.title}
          body={`This permanently deletes ${deleteTarget.title} and its audio file. This can't be undone.`}
          failure={`Couldn't confirm deletion of ${deleteTarget.title}. Try again.`}
          remove={(password) => deleteCue(deleteTarget, password, fetchImpl)}
          onCancel={() => setDeleteTarget(null)}
          onDone={(outcome) => finishDelete(deleteTarget, outcome)}
        />
      )}
    </div>
  )
}
