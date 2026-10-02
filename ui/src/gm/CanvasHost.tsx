/**
 * CanvasHost -- the Workbench's canvas column (agent-forge-harness-1kg.6.3, brief 2.6).
 *
 * It reads the canvas state the provider holds and renders it:
 *
 * - `open`: `CanvasPane` around a READ-ONLY `GameDocument` (I-1: nothing is editable
 *   until 1kg.6.5 gives it a save path, so there is no Edit, no field assistant and no
 *   SelectionBar). The title is the document's `data.name`.
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
 * Its state is keyed by document id and write revision, so a page that arrives for a
 * document that has since been replaced is dropped with the component that asked.
 *
 * The pane's width mode follows THIS COLUMN's measured width (below 560 px is
 * \`compact\`), not the viewport: the header is laid out for the room it has. At the
 * narrow layout the pane is full screen. Nothing here uses a width media query; the
 * shell's boundaries live in \`breakpoints.ts\`.
 *
 * Nothing here writes. A title, a summary or field text is GM-private (X-7): it is
 * rendered and nothing more.
 *
 * REVEAL-13 / I-8: this base has no reveal-write route, so nothing can be revealed and
 * the document's own default badge, \`GM ONLY\`, is true. 1kg.7.3 replaces it with the
 * server's live projection (the badge, and the pane's \`reveal\`).
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import { useShellLayout } from '../shell/breakpoints'
import { useCanvasActions, useCanvasState } from '../shell/canvasContext'
import { WORKBENCH_COPY } from '../shell/workbenchCopy'
import { CanvasPane, type CanvasLayout } from './CanvasPane'
import type { Document, DocumentVersion } from './contracts'
import { getDocumentHistory } from './documentApi'
import { documentTitle } from './documentTitle'
import { GameDocument } from './GameDocument'
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

interface OpenCanvasProps {
  document: Document
  layout: CanvasLayout
  fetchImpl: typeof fetch | undefined
}

/** One open document. Keyed by id and write revision, so its history state is always its own. */
function OpenCanvas({ document, layout, fetchImpl }: OpenCanvasProps): React.JSX.Element {
  const { closeDocument, openerRef, composerRef, titleRef } = useCanvasActions()
  const [historyOpen, setHistoryOpen] = React.useState(false)
  const [history, setHistory] = React.useState<HistoryState | null>(null)
  const mounted = React.useRef(true)
  React.useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const { campaign_id: campaignId, document_id: documentId } = document

  const loadFirstPage = React.useCallback(async (): Promise<void> => {
    setHistory(HISTORY_LOADING)
    const result = await getDocumentHistory(campaignId, documentId, null, fetchImpl)
    if (!mounted.current) return
    setHistory(
      result.kind === 'ok'
        ? { status: 'ready', versions: result.page.items, nextCursor: result.page.next_cursor, loadingMore: false }
        : { status: 'error', versions: [], nextCursor: null, loadingMore: false },
    )
  }, [campaignId, documentId, fetchImpl])

  const loadMore = React.useCallback(async (): Promise<void> => {
    const current = history
    if (current === null || current.status !== 'ready' || current.nextCursor === null || current.loadingMore) return
    setHistory({ ...current, loadingMore: true })
    const result = await getDocumentHistory(campaignId, documentId, current.nextCursor, fetchImpl)
    if (!mounted.current) return
    if (result.kind !== 'ok') {
      // The rows already shown stay, and Load more is still there to press again.
      setHistory({ ...current, loadingMore: false })
      return
    }
    const shown = new Set(current.versions.map((version) => version.number))
    setHistory({
      status: 'ready',
      versions: [...current.versions, ...result.page.items.filter((version) => !shown.has(version.number))],
      nextCursor: result.page.next_cursor,
      loadingMore: false,
    })
  }, [history, campaignId, documentId, fetchImpl])

  function toggleHistory(): void {
    const next = !historyOpen
    setHistoryOpen(next)
    if (next && history === null) void loadFirstPage()
  }

  const read = history ?? HISTORY_LOADING
  return (
    <CanvasPane
      title={documentTitle(document)}
      documentType={document.type}
      updatedAt={document.updated_at}
      versionNumber={document.version.number}
      saveStatus="saved"
      historyOpen={historyOpen}
      onToggleHistory={toggleHistory}
      history={{
        versions: read.versions,
        currentVersionNumber: document.version.number,
        status: read.status,
        hasMore: read.nextCursor !== null,
        loadingMore: read.loadingMore,
        onLoadMore: () => void loadMore(),
        onRetry: () => void loadFirstPage(),
        documentType: document.type,
      }}
      onRequestClose={closeDocument}
      openerRef={openerRef}
      composerRef={composerRef}
      titleRef={titleRef}
      layout={layout}
    >
      <GameDocument document={document} readOnly />
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
  const shellLayout = useShellLayout()
  const rootRef = React.useRef<HTMLDivElement>(null)
  const width = useColumnWidth(rootRef, doc.kind !== 'closed')
  if (doc.kind === 'closed') return null

  // CANVAS-32: a panel's Close returns focus the way the pane's own Close does.
  const close = async (): Promise<void> => {
    if (await closeDocument()) returnFocus(openerRef.current, composerRef.current)
  }

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
          key={`${doc.document.document_id}:${doc.document.write_revision}`}
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
