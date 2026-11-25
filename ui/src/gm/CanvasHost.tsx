/**
 * CanvasHost -- the Workbench's canvas column (agent-forge-harness-1kg.6.3, brief 2.6).
 *
 * It reads the canvas state the provider holds and renders it:
 *
 * - `open`: `CanvasPane` around a `GameDocument`. Direct edits are saved per field;
 *   archived documents are read-only. The title is the document's `data.name`.
 * - `loading`: a skeleton and visible text, `aria-busy`, and no live region of its own
 *   (A-29: the shell's one announcer carries canvas-level outcomes).
 * - `unavailable`, `unsupported`, `failed`: a plain panel. `unavailable` is one state
 *   that never says whether the document was deleted or is somebody else's (CANVAS-31);
 *   `unsupported` is a placeholder for a newer document (X-8); `failed` offers Retry and
 *   says nothing was lost (STATE-5). Each heading is a programmatic focus target.
 * - `closed`: nothing at all.
 *
 * The version history is read here, on demand: one \`GET versions?limit=20\` when the
 * disclosure first opens for a document, one more per Load more, never one per version.
 * History is cached for the current write revision. Editor state survives saves.
 *
 * The pane's width mode follows THIS COLUMN's measured width (below 560 px is
 * \`compact\`), not the viewport: the header is laid out for the room it has. At the
 * narrow layout the pane is full screen. Nothing here uses a width media query; the
 * shell's boundaries live in \`breakpoints.ts\`.
 *
 * Lifecycle (1kg.6.4, PR-2): an open document that is archived stays open under an
 * `Archived` banner with **Restore** (LIB-16), and Restore re-reads it so the banner is
 * the server's word. The unavailable and failed panels offer **Back to library** (section
 * 12.2) beside Close.
 *
 * REVEAL-13 (1kg.7.3): the header, the badge and the field markers say what the server's
 * reveal picture says (`useReveals()`), through `revealPresentation`. While the first
 * picture loads the header says so, and when it cannot be read it says `Reveal state
 * unknown — reconnecting` with no Open control: `GM ONLY` is shown only once the picture
 * confirms the document is not live. Open starts the reveal sheet (`RevealSheetHost`,
 * mounted at the shell root); Stop showing is sent at once and moves no focus (X-3).
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import '../ds/Button.css'
import { useShellLayout } from '../shell/breakpoints'
import { useCanvasActions, useCanvasState } from '../shell/canvasContext'
import { useLibraryPanel } from '../shell/libraryPanel'
import { useReveals } from '../shell/revealContext'
import { LIBRARY_COPY, WORKBENCH_COPY } from '../shell/workbenchCopy'
import { CanvasPane, type CanvasLayout } from './CanvasPane'
import { versionLabel } from './canvasStatus'
import type { Document, DocumentVersion } from './contracts'
import { getDocumentHistory, restoreDocumentVersion, unarchiveDocument } from './documentApi'
import { DocumentAutosave } from './documentAutosave'
import { documentFieldReads } from './documentFields'
import { documentTitle } from './documentTitle'
import { GameDocument } from './GameDocument'
import { PortraitUpload } from './PortraitUpload'
import { documentTypeById } from './registry'
import { NO_REVEAL_CONTROLS, revealPresentation } from './revealPresentation'
import { returnFocus } from './returnFocus'
import './CanvasHost.css'

/** §10.2: below this much canvas width the pane's header goes `compact`. */
const COMPACT_BELOW_PX = 560

interface HistoryState {
  readonly status: 'loading' | 'ready' | 'error'
  readonly versions: readonly DocumentVersion[]
  readonly nextCursor: string | null
  readonly loadingMore: boolean
}

const HISTORY_LOADING: HistoryState = { status: 'loading', versions: [], nextCursor: null, loadingMore: false }

/** This column's content width, or null until measured (and always where ResizeObserver is missing). */
function useColumnWidth(ref: React.RefObject<HTMLElement | null>, enabled: boolean): number | null {
  const [width, setWidth] = React.useState<number | null>(null)
  React.useEffect(() => {
    const element = ref.current
    if (!enabled || element === null || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const entry = entries.at(-1)
      if (entry !== undefined) setWidth(entry.contentRect.width)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref, enabled])
  return width
}

type RestoreState = 'idle' | 'busy' | 'throttled' | 'failed'

/** LIB-16: an archived document that is open says so, and Restore brings it back (the document only, never a reveal). */
function ArchivedBanner({
  document, title, fetchImpl,
}: { document: Document; title: string; fetchImpl: typeof fetch | undefined }): React.JSX.Element {
  const { bumpDocumentsVersion, refreshDocument, announce, titleRef } = useCanvasActions()
  const [state, setState] = React.useState<RestoreState>('idle')
  const { campaign_id: campaignId, document_id: documentId } = document

  const restore = async (): Promise<void> => {
    if (state === 'busy') return
    setState('busy')
    const result = await unarchiveDocument(campaignId, documentId, fetchImpl)
    if (result.kind === 'ok' || result.kind === 'unavailable') {
      if (result.kind === 'ok') {
        bumpDocumentsVersion()
        announce(LIBRARY_COPY.restored(title))
      } else {
        announce(LIBRARY_COPY.gone)
      }
      // The banner is the server's word: read the document again, which also removes this banner.
      await refreshDocument(documentId)
      setState('idle')
      titleRef.current?.focus()
    } else if (result.kind === 'throttled') {
      setState('throttled')
    } else if (result.kind === 'failed') {
      setState('failed')
    } else {
      setState('idle')
    }
  }

  return (
    <div role="group" aria-label={WORKBENCH_COPY.archivedRegion} className="canvas-host__banner">
      <span className="material-symbols-rounded canvas-host__banner-icon" aria-hidden="true">
        inventory_2
      </span>
      <div className="canvas-host__banner-text">
        <p className="canvas-host__banner-line">{WORKBENCH_COPY.archivedBanner}</p>
        {state === 'throttled' && <p className="canvas-host__banner-note">{LIBRARY_COPY.throttled}</p>}
        {state === 'failed' && <p className="canvas-host__banner-note">{LIBRARY_COPY.restoreFailed(title)}</p>}
      </div>
      <button
        type="button"
        className="aether-btn"
        data-variant="tonal"
        data-size="medium"
        data-touch-target="true"
        aria-disabled={state === 'busy' || undefined}
        onClick={() => void restore()}
      >
        <span className="aether-btn__state" aria-hidden="true" />
        <span className="aether-btn__label">{state === 'busy' ? LIBRARY_COPY.restoring : LIBRARY_COPY.restore}</span>
      </button>
    </div>
  )
}

interface OpenCanvasProps {
  document: Document
  layout: CanvasLayout
  fetchImpl: typeof fetch | undefined
}

/** One open document. A pending field remains mounted when an earlier write lands. */
function OpenCanvas({ document, layout, fetchImpl }: OpenCanvasProps): React.JSX.Element {
  const { closeDocument, openerRef, composerRef, titleRef, registerDirtySource, acceptDocument, flushBeforeLeaving, announce } = useCanvasActions()
  const [autosave] = React.useState(() => new DocumentAutosave(document, acceptDocument, fetchImpl))
  const saves = React.useSyncExternalStore(autosave.subscribe, autosave.getSnapshot)
  React.useLayoutEffect(() => {
    const stop = autosave.mount()
    const unregister = registerDirtySource(autosave)
    return () => {
      unregister()
      stop()
    }
  }, [autosave, registerDirtySource])
  React.useLayoutEffect(() => autosave.sync(document), [autosave, document])

  const reveals = useReveals()
  const [historyOpen, setHistoryOpen] = React.useState(false)
  const [history, setHistory] = React.useState<(HistoryState & { revision: number }) | null>(null)
  const restoreInProgress = React.useRef(false)
  const [restoring, setRestoring] = React.useState<number | null>(null)
  const [restoreError, setRestoreError] = React.useState<string | null>(null)
  const historyRequest = React.useRef(0)
  const requestedRevision = React.useRef<number | null>(null)
  const mounted = React.useRef(true)
  React.useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const { campaign_id: campaignId, document_id: documentId, write_revision: revision } = document

  const loadFirstPage = React.useCallback(async (): Promise<void> => {
    const request = ++historyRequest.current
    requestedRevision.current = revision
    const result = await getDocumentHistory(campaignId, documentId, null, fetchImpl)
    if (!mounted.current || request !== historyRequest.current) return
    setHistory(
      result.kind === 'ok'
        ? { revision, status: 'ready', versions: result.page.items, nextCursor: result.page.next_cursor, loadingMore: false }
        : { revision, status: 'error', versions: [], nextCursor: null, loadingMore: false },
    )
  }, [campaignId, documentId, fetchImpl, revision])

  const loadMore = React.useCallback(async (): Promise<void> => {
    const current = history
    if (current === null || current.revision !== revision || current.status !== 'ready' ||
        current.nextCursor === null || current.loadingMore) return
    const request = ++historyRequest.current
    setHistory({ ...current, loadingMore: true })
    const result = await getDocumentHistory(campaignId, documentId, current.nextCursor, fetchImpl)
    if (!mounted.current || request !== historyRequest.current) return
    if (result.kind !== 'ok') {
      // The rows already shown stay, and Load more is still there to press again.
      setHistory({ ...current, loadingMore: false })
      return
    }
    const shown = new Set(current.versions.map((version) => version.number))
    setHistory({
      revision,
      status: 'ready',
      versions: [...current.versions, ...result.page.items.filter((version) => !shown.has(version.number))],
      nextCursor: result.page.next_cursor,
      loadingMore: false,
    })
  }, [history, campaignId, documentId, fetchImpl, revision])

  React.useEffect(() => {
    if (historyOpen && requestedRevision.current !== revision) void loadFirstPage()
  }, [revision, historyOpen, loadFirstPage])

  function toggleHistory(): void {
    setHistoryOpen(!historyOpen)
  }

  async function restoreVersion(version: DocumentVersion): Promise<void> {
    if (restoreInProgress.current) return
    restoreInProgress.current = true
    const opener = window.document.activeElement
    try {
      if (!(await flushBeforeLeaving()) || !mounted.current) return
      setRestoreError(null)
      setRestoring(version.number)
      const result = await restoreDocumentVersion(document, version.number, fetchImpl)
      if (!mounted.current) return
      const label = versionLabel(version.number)
      if (result.kind === 'ok') {
        acceptDocument(result.document)
        announce(`Restored ${label}`)
        if (window.document.activeElement === opener || window.document.activeElement === window.document.body) {
          titleRef.current?.focus({ preventScroll: true })
        }
      } else {
        setRestoreError(`Couldn't restore ${label}. Select Restore ${label} to try again.`)
      }
    } finally {
      restoreInProgress.current = false
      if (mounted.current) setRestoring(null)
    }
  }

  const read = history?.revision === revision ? history : HISTORY_LOADING
  const title = documentTitle(document)
  const type = documentTypeById(document.type)
  const canEditPortrait = !document.archived && restoring === null &&
    documentFieldReads(document.type).some((field) => field.key === 'portrait' && field.editable)
  const presentation =
    type === undefined ? NO_REVEAL_CONTROLS : revealPresentation(reveals, documentId, type, reveals.seats ?? [])

  async function openReveal(): Promise<void> {
    const opener = window.document.activeElement
    if (await flushBeforeLeaving() && mounted.current) reveals.openSheet(documentId, opener)
  }

  // Pressing Stop moves no focus (X-3). But once the table is no longer seeing the document the Stop control is
  // gone, and a browser drops focus from a control that unmounts to <body>: the canvas heading, which every state
  // makes a programmatic focus target, takes it then, and only if it fell.
  const stopPressed = React.useRef(false)
  const canStop = presentation.canStop
  React.useEffect(() => {
    if (canStop || !stopPressed.current) return
    stopPressed.current = false
    const active = window.document.activeElement
    if (active === null || active === window.document.body) titleRef.current?.focus()
  }, [canStop, titleRef])
  return (
    <CanvasPane
      title={title}
      documentType={document.type}
      updatedAt={document.updated_at}
      versionNumber={document.version.number}
      saveStatus={saves.status}
      onRetrySave={saves.retryable ? () => autosave.retry() : undefined}
      historyOpen={historyOpen}
      onToggleHistory={toggleHistory}
      history={{
        versions: read.versions,
        currentVersionNumber: document.version.number,
        status: read.status,
        hasMore: read.nextCursor !== null,
        loadingMore: read.loadingMore,
        onLoadMore: () => void loadMore(),
        onRetry: () => {
          setHistory(null)
          void loadFirstPage()
        },
        onRestore: document.archived ? undefined : (version) => void restoreVersion(version),
        restoringVersionNumber: restoring,
        restoreError,
        documentType: document.type,
      }}
      onRequestClose={closeDocument}
      openerRef={openerRef}
      composerRef={composerRef}
      titleRef={titleRef}
      layout={layout}
      reveal={presentation.reveal}
      onReveal={presentation.canOpen && restoring === null ? () => void openReveal() : undefined}
      onStopReveal={
        presentation.canStop
          ? () => {
              stopPressed.current = true
              reveals.stop(documentId, title)
            }
          : undefined
      }
    >
      {document.archived && <ArchivedBanner document={document} title={title} fetchImpl={fetchImpl} />}
      {saves.status === 'conflict' && (
        <p className="canvas-host__banner">This document changed elsewhere</p>
      )}
      <GameDocument
        key={saves.reset}
        document={document}
        readOnly={document.archived || restoring !== null}
        fieldStates={saves.fields}
        onFieldDraft={autosave.draft}
        onInvalidFieldDraft={autosave.invalidDraft}
        onCancelFieldDraft={autosave.cancel}
        onFieldCommit={autosave.commit}
        onRetry={autosave.retry}
        onKeepMine={autosave.keepMine}
        onUseLatest={autosave.cancel}
        portraitEditor={canEditPortrait ? (
          <PortraitUpload
            campaignId={campaignId}
            hasPortrait={document.data.portrait != null}
            locked={saves.fields.portrait !== undefined}
            fetchImpl={fetchImpl}
            onStart={() => autosave.invalidDraft('portrait')}
            onCancel={() => autosave.cancel('portrait')}
            onAttach={(asset) => autosave.commit('portrait', asset)}
          />
        ) : undefined}
        selectionEnabled={false}
        revealBadge={presentation.badge}
        revealedFields={presentation.fields}
        revealedNote={presentation.note}
      />
    </CanvasPane>
  )
}

interface PanelProps {
  heading: string
  body?: string
  children: React.ReactNode
}

/** A column that is not a document: a heading to focus, what happened, and what can be done. */
function Panel({ heading, body, children }: PanelProps): React.JSX.Element {
  const headingId = React.useId()
  const { titleRef } = useCanvasActions()
  return (
    <section className="canvas-host__panel" aria-labelledby={headingId}>
      <h2 className="canvas-host__heading" id={headingId} ref={titleRef} tabIndex={-1}>
        {heading}
      </h2>
      {body !== undefined && <p className="canvas-host__body">{body}</p>}
      <div className="canvas-host__actions">{children}</div>
    </section>
  )
}

export interface CanvasHostProps {
  /** For tests: the `fetch` the history reads go through. */
  fetchImpl?: typeof fetch
}

export function CanvasHost({ fetchImpl }: CanvasHostProps): React.JSX.Element | null {
  const { doc } = useCanvasState()
  const { closeDocument, retry, openerRef, composerRef } = useCanvasActions()
  const library = useLibraryPanel()
  const shellLayout = useShellLayout()
  const rootRef = React.useRef<HTMLDivElement>(null)
  const width = useColumnWidth(rootRef, doc.kind !== 'closed')
  if (doc.kind === 'closed') return null

  // CANVAS-32: a panel's Close returns focus the way the pane's own Close does.
  const close = async (): Promise<void> => {
    if (await closeDocument()) returnFocus(openerRef.current, composerRef.current)
  }

  // Section 12.2: the way out of a state that is not a document is the library, on the tab last used.
  const backToLibrary = (
    <Button variant="text" icon="arrow_back" onClick={(event) => library.openLibrary(library.category, event.currentTarget)}>
      {WORKBENCH_COPY.backToLibrary}
    </Button>
  )

  const layout: CanvasLayout =
    shellLayout === 'narrow' ? 'fullScreen' : width !== null && width < COMPACT_BELOW_PX ? 'compact' : 'wide'

  let body: React.JSX.Element
  switch (doc.kind) {
    case 'loading':
      body = (
        <div className="canvas-host__loading">
          <div className="canvas-host__skeleton" aria-hidden="true">
            <span className="canvas-host__bar" />
            <span className="canvas-host__bar" />
            <span className="canvas-host__bar" />
          </div>
          <p className="canvas-host__opening">{WORKBENCH_COPY.opening(doc.title)}</p>
        </div>
      )
      break
    case 'open':
      body = (
        <OpenCanvas
          key={`${doc.document.campaign_id}:${doc.document.document_id}`}
          document={doc.document}
          layout={layout}
          fetchImpl={fetchImpl}
        />
      )
      break
    case 'unavailable':
      body = (
        <Panel heading={WORKBENCH_COPY.unavailableHeading} body={WORKBENCH_COPY.unavailableBody}>
          <Button variant="filled" onClick={() => void close()}>
            {WORKBENCH_COPY.close}
          </Button>
          {backToLibrary}
        </Panel>
      )
      break
    case 'unsupported':
      body = (
        <Panel heading={WORKBENCH_COPY.unsupportedHeading}>
          <Button variant="filled" onClick={() => void close()}>
            {WORKBENCH_COPY.close}
          </Button>
        </Panel>
      )
      break
    case 'failed':
      body = (
        <Panel heading={WORKBENCH_COPY.failedHeading(doc.title)} body={WORKBENCH_COPY.failedBody}>
          <Button variant="filled" onClick={retry}>
            {WORKBENCH_COPY.retry}
          </Button>
          <Button variant="text" onClick={() => void close()}>
            {WORKBENCH_COPY.close}
          </Button>
          {backToLibrary}
        </Panel>
      )
      break
  }

  return (
    <div
      ref={rootRef}
      className="canvas-host"
      data-layout={layout}
      aria-busy={doc.kind === 'loading' ? 'true' : undefined}
    >
      {body}
    </div>
  )
}
