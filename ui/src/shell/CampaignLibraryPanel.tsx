/**
 * CampaignLibraryPanel -- the Campaign Library (agent-forge-harness-1kg.6.4; brief 2.4;
 * interactions ADR LIB-1 to LIB-26, section 12.2).
 *
 * A 320 px NON-MODAL panel beside the navigation, holding four tabs (NPCs, Bestiary,
 * Documents, Session log; no Cues tab, I-1) over server-side lists: search, sort, the
 * Active / Archived filter, the Documents type filter and Load more. A row opens its
 * document into the canvas; Archived rows can be Restored; a category can make a new
 * document by hand (everything except a stat block, I-2).
 *
 * - Non-modal: no focus trap, no scrim, no `aria-modal` (LAYOUT-10). Escape closes the
 *   New disclosure first, then the panel; focus then goes back to the opener
 *   (`libraryPanel`). The shell owns the geometry, by `data-layout` and `data-nav`.
 * - Titles, search text and field text are GM-private (X-7): rendered and announced,
 *   never a URL, a key, a class or a log field.
 * - One status node, mounted EMPTY for the panel's life, takes the rationed
 *   announcements (STATE-7, A-29): a search that settles, Load more, the first page's
 *   error, and Restore and create outcomes. Loading is visible text, never announced.
 * - Each tab is its own component instance (`key`), so a switch resets the search, sort,
 *   filter and type (I-6) and drops every request and in-flight answer of the old tab.
 */

import * as React from 'react'
import { documentTypeRead } from '../gm/canvasStatus'
import { SEARCH_MAX_CHARS, type DocumentTypeId } from '../gm/contracts'
import { createDocument, unarchiveDocument } from '../gm/documentApi'
import { REGISTRY } from '../gm/registry'
import type { ShellLayout } from './breakpoints'
import { useCampaign } from './campaignContext'
import { PendingButton, useRetrying } from './CampaignPicker'
import { useCanvasActions, useCanvasState, type CanvasDoc } from './canvasContext'
import { LIBRARY_HEADING_ID, LIBRARY_PANEL_ID, LIBRARY_TABS, useLibraryPanel, type LibraryCategoryId } from './libraryPanel'
import { mintCommandId } from './tableSessionApi'
import { useLibraryList, type LibraryListState } from './useLibraryList'
import { LIBRARY_COPY, WORKBENCH_COPY } from './workbenchCopy'
import { TextField } from '../ds/TextField'
import { Chip } from '../ds/Chip'
import '../ds/Button.css'
import './CampaignLibraryPanel.css'

const NBSP = ' '
const SKELETON_ROWS = 3
const NO_ITEMS: readonly never[] = []

function currentDocumentId(doc: CanvasDoc): string | null {
  if (doc.kind === 'closed') return null
  return doc.kind === 'open' ? doc.document.document_id : doc.documentId
}

const tabId = (category: LibraryCategoryId): string => `${LIBRARY_PANEL_ID}-tab-${category}`
const TABPANEL_ID = `${LIBRARY_PANEL_ID}-tabpanel`

export interface CampaignLibraryPanelProps {
  /** The shell's layout: the Back control and where a document leaves the panel. */
  layout: ShellLayout
  /** The shell sets this while the nav drawer is open: the panel is `inert` and ignores Escape. */
  inert?: boolean
  /** For tests: the `fetch` the list, create and restore calls go through. */
  fetchImpl?: typeof fetch
}

/** The panel renders only while open (the shell decides); it focuses its heading when it mounts. */
export function CampaignLibraryPanel({ layout, inert = false, fetchImpl }: CampaignLibraryPanelProps): React.JSX.Element {
  const { category, setCategory, closeLibrary } = useLibraryPanel()
  const { guardDialog } = useCanvasState()
  const headingRef = React.useRef<HTMLHeadingElement>(null)
  const tabRefs = React.useRef(new Map<LibraryCategoryId, HTMLButtonElement>())
  const [announcement, setAnnouncement] = React.useState({ text: '', tick: 0 })
  const announce = React.useCallback((text: string): void => {
    setAnnouncement((now) => ({ text, tick: now.tick + 1 }))
  }, [])

  React.useEffect(() => {
    headingRef.current?.focus()
  }, [])

  // Escape closes the panel from anywhere in the document, after every inner surface that handled it
  // (the New disclosure, the loss-guard dialog) has called preventDefault. While the nav drawer or the
  // loss-guard dialog is open the panel is not the surface that answers it.
  const suspended = inert || guardDialog !== null
  React.useEffect(() => {
    if (suspended) return
    function handleEscape(event: KeyboardEvent): void {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      closeLibrary({ returnFocus: true })
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [suspended, closeLibrary])

  function handleTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number): void {
    const last = LIBRARY_TABS.length - 1
    let next: number
    if (event.key === 'ArrowRight') next = index === last ? 0 : index + 1
    else if (event.key === 'ArrowLeft') next = index === 0 ? last : index - 1
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = last
    else return
    event.preventDefault()
    const target = LIBRARY_TABS[next]
    setCategory(target)
    tabRefs.current.get(target)?.focus()
  }

  return (
    <section
      id={LIBRARY_PANEL_ID}
      className="library-panel"
      aria-labelledby={LIBRARY_HEADING_ID}
      data-layout={layout}
      inert={inert || undefined}
    >
      <div className="library-panel__head">
        {layout === 'narrow' && (
          <button type="button" className="library-panel__back" onClick={() => closeLibrary({ returnFocus: true })}>
            <span className="material-symbols-rounded" aria-hidden="true">
              arrow_back
            </span>
            <span>{LIBRARY_COPY.back}</span>
          </button>
        )}
        <h2 id={LIBRARY_HEADING_ID} ref={headingRef} className="library-panel__heading" tabIndex={-1}>
          {LIBRARY_COPY.title}
        </h2>
        {layout !== 'narrow' && (
          <button
            type="button"
            className="library-panel__close"
            aria-label={LIBRARY_COPY.close}
            onClick={() => closeLibrary({ returnFocus: true })}
          >
            <span className="material-symbols-rounded" aria-hidden="true">
              close
            </span>
          </button>
        )}
      </div>

      <div role="tablist" aria-label={LIBRARY_COPY.tabs} className="library-panel__tabs">
        {LIBRARY_TABS.map((id, index) => {
          const selected = id === category
          return (
            <button
              key={id}
              ref={(node) => {
                if (node === null) tabRefs.current.delete(id)
                else tabRefs.current.set(id, node)
              }}
              type="button"
              role="tab"
              id={tabId(id)}
              className="library-panel__tab"
              aria-selected={selected}
              aria-controls={TABPANEL_ID}
              tabIndex={selected ? 0 : -1}
              onClick={() => setCategory(id)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {LIBRARY_COPY.category[id].tab}
            </button>
          )
        })}
      </div>

      <div role="tabpanel" id={TABPANEL_ID} aria-labelledby={tabId(category)} className="library-panel__body">
        <LibraryTabBody key={category} category={category} layout={layout} announce={announce} headingRef={headingRef} fetchImpl={fetchImpl} />
      </div>

      {/* Mounted empty, and kept: a live region that appears with its text is not reliably announced (A-29). */}
      <p role="status" className="library-panel__sr-only">
        {announcement.text === '' ? '' : announcement.text + (announcement.tick % 2 === 0 ? NBSP : '')}
      </p>
    </section>
  )
}

// ── One button, the DS's markup ──────────────────────────────────────────────

interface ActionButtonProps {
  children: React.ReactNode
  variant?: 'tonal' | 'text'
  icon?: string
  /** Set only when it starts with the visible text (WCAG 2.5.3). */
  ariaLabel?: string
  /** Its own request is in flight: `aria-disabled`, and a press does nothing. */
  busy?: boolean
  onPress: () => void
  buttonRef?: React.Ref<HTMLButtonElement>
  expanded?: boolean
  controls?: string
}

/** The DS button, but `aria-disabled` rather than `disabled` while busy (a disabled button drops focus),
 * and with an accessible name that can carry the row's title. */
function ActionButton({
  children, variant = 'text', icon, ariaLabel, busy = false, onPress, buttonRef, expanded, controls,
}: ActionButtonProps): React.JSX.Element {
  return (
    <button
      ref={buttonRef}
      type="button"
      className="aether-btn"
      data-variant={variant}
      data-size="medium"
      data-touch-target="true"
      aria-label={ariaLabel}
      aria-disabled={busy || undefined}
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={() => {
        if (!busy) onPress()
      }}
    >
      <span className="aether-btn__state" aria-hidden="true" />
      {icon !== undefined && (
        <span className="material-symbols-rounded" aria-hidden="true">
          {icon}
        </span>
      )}
      <span className="aether-btn__label">{children}</span>
    </button>
  )
}

// ── One tab ──────────────────────────────────────────────────────────────────

type RestoreNote =
  | { readonly kind: 'busy'; readonly id: string }
  | { readonly kind: 'throttled' | 'failed'; readonly id: string; readonly title: string }
  | { readonly kind: 'gone' }

type CreateError = 'full' | 'throttled' | 'failed' | 'refused'

interface LibraryTabBodyProps {
  category: LibraryCategoryId
  layout: ShellLayout
  announce: (text: string) => void
  /** Where focus lands if the control that had it leaves the DOM. */
  headingRef: React.RefObject<HTMLElement | null>
  fetchImpl?: typeof fetch
}

const SEARCH_FIELD = 'search'

function LibraryTabBody({ category, layout, announce, headingRef, fetchImpl }: LibraryTabBodyProps): React.JSX.Element {
  const { scope } = useCampaign()
  const { doc } = useCanvasState()
  const { openDocument, bumpDocumentsVersion } = useCanvasActions()
  const { closeLibrary } = useLibraryPanel()
  const campaignId = scope?.campaignId ?? null
  const words = LIBRARY_COPY.category[category]

  const [search, setSearch] = React.useState('')
  const [sort, setSort] = React.useState<'recent' | 'name'>('recent')
  const [archived, setArchived] = React.useState(false)
  const [type, setType] = React.useState<DocumentTypeId | null>(null)
  const list = useLibraryList({ category, search, sort, archived, type }, fetchImpl)
  const { state } = list

  const searchRef = React.useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const rowRefs = React.useRef(new Map<string, HTMLButtonElement>())
  const lastRowRef = React.useRef<HTMLButtonElement | null>(null)
  const focusAfter = React.useRef<string | null>(null)
  const searchErrorId = React.useId()
  const menuId = React.useId()
  const newRef = React.useRef<HTMLButtonElement>(null)

  React.useLayoutEffect(() => {
    // The registry's own cap, so a field that cannot take more never reaches `invalid` for length.
    if (searchRef.current instanceof HTMLInputElement) searchRef.current.maxLength = SEARCH_MAX_CHARS
  }, [])

  const items = state.status === 'ready' ? state.items : NO_ITEMS

  // After a Restore removed a row, focus goes to the next row, else the previous, else the search field.
  React.useLayoutEffect(() => {
    const target = focusAfter.current
    if (target === null) return
    focusAfter.current = null
    if (target === SEARCH_FIELD) searchRef.current?.focus()
    else rowRefs.current.get(target)?.focus()
  }, [items])

  // ── Announcements: one status node, rationed (STATE-7) ──
  const generation = state.status === 'ready' ? state.generation : 0
  const more = state.status === 'ready' ? state.more : 'idle'
  const settledSearch = state.status === 'ready' ? state.search : ''
  const hasNext = state.status === 'ready' && state.nextCursor !== null
  const count = items.length
  const seen = React.useRef({ status: state.status, generation: 0, more: 'idle' as string, count: 0 })
  React.useEffect(() => {
    const previous = seen.current
    seen.current = { status: state.status, generation, more, count }
    if (state.status === 'error' && previous.status !== 'error') announce(LIBRARY_COPY.loadFailed(words.noun))
    else if (generation !== previous.generation) {
      if (settledSearch !== '') announce(LIBRARY_COPY.found(count, hasNext))
    } else if (previous.more === 'loading' && more === 'idle') announce(LIBRARY_COPY.moreLoaded(count - previous.count))
    else if (previous.more === 'loading' && more === 'failed') announce(LIBRARY_COPY.moreFailed)
  }, [state.status, generation, more, count, settledSearch, hasNext, announce, words.noun])

  const closeBehindDocument = (): void => {
    if (layout !== 'wide') closeLibrary({ returnFocus: false })
  }

  // ── Restore ──
  const [restore, setRestore] = React.useState<RestoreNote | null>(null)
  const restoringId = restore?.kind === 'busy' ? restore.id : null

  const onRestore = async (id: string, title: string): Promise<void> => {
    if (campaignId === null || restoringId !== null) return
    setRestore({ kind: 'busy', id })
    const result = await unarchiveDocument(campaignId, id, fetchImpl)
    if (result.kind === 'ok' || result.kind === 'unavailable') {
      const index = items.findIndex((item) => item.document_id === id)
      focusAfter.current = items[index + 1]?.document_id ?? items[index - 1]?.document_id ?? SEARCH_FIELD
      list.removeItem(id)
      if (result.kind === 'ok') {
        setRestore(null)
        announce(LIBRARY_COPY.restored(title))
      } else {
        setRestore({ kind: 'gone' })
        announce(LIBRARY_COPY.gone)
      }
    } else if (result.kind === 'throttled') {
      setRestore({ kind: 'throttled', id, title })
      announce(LIBRARY_COPY.throttled)
    } else if (result.kind === 'failed') {
      setRestore({ kind: 'failed', id, title })
      announce(LIBRARY_COPY.restoreFailed(title))
    } else {
      setRestore(null)
    }
  }

  // ── New ──
  const creatable = React.useMemo(
    () => REGISTRY.document_types.filter((t) => t.library_category === category && t.id !== 'statblock'),
    [category],
  )
  const [menuOpen, setMenuOpen] = React.useState(false)
  const [creating, setCreating] = React.useState<DocumentTypeId | null>(null)
  const [createError, setCreateError] = React.useState<{ readonly error: CreateError; readonly type: DocumentTypeId } | null>(null)
  /** One id per intent, kept until it succeeds, so a retry opens the document already made (STATE-3). */
  const intent = React.useRef<{ readonly type: DocumentTypeId; readonly commandId: string } | null>(null)

  const onCreate = async (typeId: DocumentTypeId): Promise<void> => {
    if (campaignId === null || creating !== null) return
    if (intent.current?.type !== typeId) intent.current = { type: typeId, commandId: mintCommandId() }
    const { commandId } = intent.current
    const name = WORKBENCH_COPY.untitled(documentTypeRead(typeId).label)
    setCreating(typeId)
    setCreateError(null)
    const result = await createDocument(campaignId, { commandId, type: typeId, name }, fetchImpl)
    setCreating(null)
    if (result.kind === 'ok') {
      intent.current = null
      setMenuOpen(false)
      bumpDocumentsVersion()
      void openDocument({ documentId: result.document.document_id, title: name }, { gesture: true })
      announce(LIBRARY_COPY.created(name))
      closeBehindDocument()
      return
    }
    if (result.kind === 'unauthorized') return
    if (result.kind === 'limit') {
      setCreateError({ error: 'full', type: typeId })
      announce(LIBRARY_COPY.storageFull)
    } else if (result.kind === 'throttled') {
      setCreateError({ error: 'throttled', type: typeId })
      announce(LIBRARY_COPY.throttled)
    } else if (result.kind === 'failed' || result.kind === 'unavailable') {
      setCreateError({ error: 'failed', type: typeId })
      announce(LIBRARY_COPY.createFailed)
    } else {
      intent.current = null
      setCreateError({ error: 'refused', type: typeId })
      announce(LIBRARY_COPY.createRefused)
    }
  }

  const createMessage: Record<CreateError, string> = {
    full: LIBRARY_COPY.storageFull,
    throttled: LIBRARY_COPY.throttled,
    failed: LIBRARY_COPY.createFailed,
    refused: LIBRARY_COPY.createRefused,
  }

  // ── Retry shown through its own read, so it is never unmounted under focus ──
  const [retryShown, pressRetry] = useRetrying(state.status === 'error', state.status === 'loading')

  const current = currentDocumentId(doc)
  const documentTypes = category === 'documents' ? REGISTRY.document_types.filter((t) => t.library_category === 'documents') : []
  const invalid = state.status === 'invalid'

  return (
    <>
      <div className="library-panel__controls">
        <TextField
          label={LIBRARY_COPY.search(words.noun)}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          leadingIcon="search"
          fullWidth
          error={invalid}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? searchErrorId : undefined}
          ref={searchRef}
        />
        {invalid && (
          <p id={searchErrorId} className="library-panel__field-error">
            {LIBRARY_COPY.searchInvalid}
          </p>
        )}

        <div className="library-panel__selects">
          <label className="library-panel__select-label">
            <span>{LIBRARY_COPY.sort}</span>
            <select
              className="library-panel__select"
              value={sort}
              onChange={(event) => setSort(event.target.value === 'name' ? 'name' : 'recent')}
            >
              <option value="recent">{LIBRARY_COPY.recent}</option>
              <option value="name">{LIBRARY_COPY.name}</option>
            </select>
          </label>
          {category === 'documents' && (
            <label className="library-panel__select-label">
              <span>{LIBRARY_COPY.type}</span>
              <select
                className="library-panel__select"
                value={type ?? ''}
                onChange={(event) => {
                  const chosen = documentTypes.find((t) => t.id === event.target.value)
                  setType(chosen === undefined ? null : chosen.id)
                }}
              >
                <option value="">{LIBRARY_COPY.allTypes}</option>
                {documentTypes.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.label}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <div role="group" aria-label={LIBRARY_COPY.show} className="library-panel__filter">
          <Chip
            type="filter"
            label={LIBRARY_COPY.active}
            selected={!archived}
            onClick={() => setArchived(false)}
            className="library-panel__chip"
          />
          <Chip
            type="filter"
            label={LIBRARY_COPY.archived}
            selected={archived}
            onClick={() => setArchived(true)}
            className="library-panel__chip"
          />
        </div>
      </div>

      <div className="library-panel__list">
        {state.status === 'loading' && <LoadingBlock noun={words.noun} />}
        {state.status === 'loading' && retryShown && (
          <PendingButton busy landing={headingRef} onPress={() => {}}>
            {LIBRARY_COPY.retry}
          </PendingButton>
        )}

        {state.status === 'error' && (
          <div className="library-panel__problem">
            <p className="library-panel__note">{LIBRARY_COPY.loadFailed(words.noun)}</p>
            <PendingButton
              busy={false}
              landing={headingRef}
              onPress={() => {
                pressRetry()
                list.retry()
              }}
            >
              {LIBRARY_COPY.retry}
            </PendingButton>
          </div>
        )}

        {state.status === 'ready' && state.items.length === 0 && (
          <EmptyBlock
            state={state}
            category={category}
            archived={archived}
            onClear={() => {
              setSearch('')
              searchRef.current?.focus()
            }}
          />
        )}

        {state.status === 'ready' && state.items.length > 0 && (
          <ul className="library-panel__rows" aria-label={words.tab}>
            {state.items.map((item, index) => {
              const isLast = index === state.items.length - 1
              const note = restore !== null && restore.kind !== 'busy' && restore.kind !== 'gone' && restore.id === item.document_id ? restore : null
              return (
                <li key={item.document_id} className="library-panel__item">
                  <div className="library-panel__item-row">
                    <button
                      type="button"
                      ref={(node) => {
                        if (node === null) rowRefs.current.delete(item.document_id)
                        else rowRefs.current.set(item.document_id, node)
                        if (isLast) lastRowRef.current = node
                      }}
                      className="library-row"
                      aria-current={current === item.document_id ? 'true' : undefined}
                      onClick={() => {
                        void openDocument({ documentId: item.document_id, title: item.title }, { gesture: true })
                        closeBehindDocument()
                      }}
                    >
                      <span className="library-row__title">{item.title}</span>
                      {item.qualifier !== '' && <span className="library-row__meta">{item.qualifier}</span>}
                      {category === 'documents' && <span className="library-row__meta">{documentTypeRead(item.type).label}</span>}
                    </button>
                    {item.archived && (
                      <ActionButton
                        ariaLabel={LIBRARY_COPY.restoreNamed(item.title)}
                        busy={restoringId !== null}
                        onPress={() => void onRestore(item.document_id, item.title)}
                      >
                        {LIBRARY_COPY.restore}
                      </ActionButton>
                    )}
                  </div>
                  {note !== null && (
                    <p className="library-panel__note library-panel__note--row">
                      {note.kind === 'throttled' ? LIBRARY_COPY.throttled : LIBRARY_COPY.restoreFailed(note.title)}
                      {note.kind === 'failed' && (
                        <PendingButton busy={false} landing={headingRef} onPress={() => void onRestore(note.id, note.title)}>
                          {LIBRARY_COPY.retry}
                        </PendingButton>
                      )}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {restore?.kind === 'gone' && <p className="library-panel__note">{LIBRARY_COPY.gone}</p>}

        {state.status === 'ready' && state.more === 'failed' && (
          <div className="library-panel__problem">
            <p className="library-panel__note">{LIBRARY_COPY.moreFailed}</p>
            <PendingButton busy={false} landing={lastRowRef} onPress={list.loadMore}>
              {LIBRARY_COPY.retry}
            </PendingButton>
          </div>
        )}
        {state.status === 'ready' && state.nextCursor !== null && state.more !== 'failed' && (
          <PendingButton busy={state.more === 'loading'} landing={lastRowRef} onPress={list.loadMore}>
            {state.more === 'loading' ? LIBRARY_COPY.loadingMore : LIBRARY_COPY.loadMore}
          </PendingButton>
        )}
      </div>

      {creatable.length > 0 && (
        <div
          className="library-panel__new"
          onKeyDown={(event) => {
            if (event.key === 'Escape' && menuOpen) {
              event.preventDefault()
              setMenuOpen(false)
              newRef.current?.focus()
            }
          }}
        >
          {creatable.length === 1 ? (
            <ActionButton
              variant="tonal"
              icon="add"
              ariaLabel={creating === null ? LIBRARY_COPY.newNamed(creatable[0].label) : undefined}
              busy={creating !== null}
              onPress={() => void onCreate(creatable[0].id)}
            >
              {creating === null ? LIBRARY_COPY.new : LIBRARY_COPY.creating}
            </ActionButton>
          ) : (
            <>
              <ActionButton
                variant="tonal"
                icon="add"
                buttonRef={newRef}
                expanded={menuOpen}
                controls={menuId}
                onPress={() => setMenuOpen((open) => !open)}
              >
                {LIBRARY_COPY.new}
              </ActionButton>
              <div id={menuId} className="library-panel__menu" hidden={!menuOpen}>
                {creatable.map((t) => (
                  <ActionButton key={t.id} busy={creating !== null} onPress={() => void onCreate(t.id)}>
                    {creating === t.id ? LIBRARY_COPY.creating : t.label}
                  </ActionButton>
                ))}
              </div>
            </>
          )}
          {createError !== null && (
            <div className="library-panel__problem">
              <p className="library-panel__note">{createMessage[createError.error]}</p>
              {(createError.error === 'throttled' || createError.error === 'failed') && (
                <PendingButton busy={creating !== null} landing={headingRef} onPress={() => void onCreate(createError.type)}>
                  {LIBRARY_COPY.retry}
                </PendingButton>
              )}
            </div>
          )}
        </div>
      )}
    </>
  )
}

function LoadingBlock({ noun }: { noun: string }): React.JSX.Element {
  return (
    <div className="library-panel__loading">
      {Array.from({ length: SKELETON_ROWS }, (_, index) => (
        <div key={index} className="library-panel__skeleton" aria-hidden="true" />
      ))}
      <p className="library-panel__note">{LIBRARY_COPY.loading(noun)}</p>
    </div>
  )
}

function EmptyBlock({
  state, category, archived, onClear,
}: {
  state: Extract<LibraryListState, { status: 'ready' }>
  category: LibraryCategoryId
  archived: boolean
  onClear: () => void
}): React.JSX.Element {
  if (state.search !== '') {
    return (
      <div className="library-panel__problem">
        <p className="library-panel__note">{LIBRARY_COPY.noMatch(state.search)}</p>
        <ActionButton onPress={onClear}>{LIBRARY_COPY.clear}</ActionButton>
      </div>
    )
  }
  const text = archived ? LIBRARY_COPY.archivedEmpty(LIBRARY_COPY.category[category].noun) : LIBRARY_COPY.empty[category]
  return <p className="library-panel__note">{text}</p>
}

