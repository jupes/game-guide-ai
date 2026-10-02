/**
 * CampaignDocumentsNav -- the nav column's "Campaign documents" list
 * (agent-forge-harness-1kg.6.3, brief 2.7; INFERRED I-2).
 *
 * The Library panel (LIB-7..10) is 1kg.6.4's. Until it lands, the GM's way to a
 * document that already exists is this small list under the campaign's threads:
 * the ten most recently changed documents of the four categories that hold
 * documents (never `cues`, which are not documents, LIB-5), one `POST
 * /campaigns/{cid}/library` per category, merged. It is self-contained so 1kg.6.4
 * can replace it whole.
 *
 * - Four requests per scope key, nothing more: not a sidebar, rail or drawer
 *   presentation change, not a canvas open or close, not a breakpoint crossing, and
 *   not StrictMode's second effect (C-13). A Retry, and a `documentsVersion` bump
 *   (a document was made or changed), ask again.
 * - The state is keyed by scope key AND request, so a switch shows none of the old
 *   campaign's rows (C-12), and a page whose echoed campaign or category is not the
 *   one asked for is dropped (LIB-25).
 * - A row is a button. Activating it opens the document as a gesture, BEFORE the
 *   drawer closes, so the canvas can record the row (or the control that opened the
 *   drawer) as the opener (C-3).
 * - Titles are GM-private (X-7): rendered, never an id, a key, a class or a URL.
 * - Nothing here is a live region (A-29): loading is visible text.
 *
 * Rendered by LeftNav only while the Workbench is active, so a player, another
 * channel and a GM with no campaign make no request.
 */

import * as React from 'react'
import { documentTypeRead } from '../gm/canvasStatus'
import type { LibraryCategory, LibraryItem } from '../gm/contracts'
import { queryLibrary } from '../gm/documentApi'
import { useCampaign } from './campaignContext'
import { useCanvasActions, useCanvasState, type CanvasDoc } from './canvasContext'
import { DOCUMENTS_HEADING_ID, WORKBENCH_COPY } from './workbenchCopy'
import './CampaignDocumentsNav.css'

/** LIB-5: cues are listed by the cue family, not here. */
const CATEGORIES = ['npcs', 'bestiary', 'documents', 'session-log'] as const satisfies readonly LibraryCategory[]
const PER_CATEGORY = 5
const SHOWN = 10

interface Loaded {
  /** The request this answers: scope key, documentsVersion and retry. */
  readonly key: string
  readonly status: 'ready' | 'error'
  readonly items: readonly LibraryItem[]
  /** At least one category failed: the error line shows beside what did load. */
  readonly partial: boolean
}

function currentDocumentId(doc: CanvasDoc): string | null {
  if (doc.kind === 'closed') return null
  return doc.kind === 'open' ? doc.document.document_id : doc.documentId
}

/** Newest first, then by id, so equal times never reshuffle between renders. */
function newestFirst(a: LibraryItem, b: LibraryItem): number {
  if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? 1 : -1
  return a.document_id < b.document_id ? -1 : a.document_id > b.document_id ? 1 : 0
}

export interface CampaignDocumentsNavProps {
  /** Called AFTER a row opens its document: the narrow and medium drawers close on it. */
  onNavigate?: () => void
  /** For tests: the `fetch` the library reads go through. */
  fetchImpl?: typeof fetch
}

export function CampaignDocumentsNav({ onNavigate, fetchImpl }: CampaignDocumentsNavProps): React.JSX.Element | null {
  const { scope } = useCampaign()
  const { doc, documentsVersion } = useCanvasState()
  const { openDocument } = useCanvasActions()
  const [retries, setRetries] = React.useState(0)
  const [loaded, setLoaded] = React.useState<Loaded | null>(null)

  const campaignId = scope?.campaignId ?? null
  const requestKey = scope === null ? null : `${scope.key}:${documentsVersion}:${retries}`

  // The key the answers are for, as of the latest render: a page that arrives under an
  // older one is dropped (a switch, a bump, a Retry). Refs, not effect cleanup, so
  // StrictMode's second effect run neither doubles the requests nor drops the answers.
  const latestKey = React.useRef<string | null>(requestKey)
  const requestedKey = React.useRef<string | null>(null)

  React.useEffect(() => {
    latestKey.current = requestKey
    if (requestKey === null || campaignId === null || requestedKey.current === requestKey) return
    requestedKey.current = requestKey
    const key = requestKey
    void Promise.all(
      CATEGORIES.map((category) =>
        queryLibrary(
          {
            schema_version: 1,
            campaign_id: campaignId,
            category,
            search: '',
            sort: 'recent',
            archived: false,
            limit: PER_CATEGORY,
          },
          fetchImpl,
        ).then((result) => ({ category, result })),
      ),
    ).then((answers) => {
      if (latestKey.current !== key) return
      const items: LibraryItem[] = []
      let failed = 0
      for (const { category, result } of answers) {
        if (result.kind !== 'ok') {
          failed += 1
        } else if (result.page.campaign_id === campaignId && result.page.category === category) {
          items.push(...result.page.items)
        }
      }
      setLoaded({
        key,
        status: failed === answers.length ? 'error' : 'ready',
        items: items.sort(newestFirst).slice(0, SHOWN),
        partial: failed > 0 && failed < answers.length,
      })
    })
  }, [requestKey, campaignId, fetchImpl])

  if (scope === null || requestKey === null) return null

  const read = loaded !== null && loaded.key === requestKey ? loaded : null
  const current = currentDocumentId(doc)
  const retry = (): void => setRetries((count) => count + 1)

  return (
    <section className="documents-nav" aria-labelledby={DOCUMENTS_HEADING_ID}>
      <h2 id={DOCUMENTS_HEADING_ID} className="documents-nav__heading" tabIndex={-1}>
        {WORKBENCH_COPY.campaignDocuments}
      </h2>

      {read === null && (
        <>
          <div className="documents-nav__skeleton" aria-hidden="true" />
          <div className="documents-nav__skeleton" aria-hidden="true" />
          <p className="documents-nav__note">{WORKBENCH_COPY.documentsLoading}</p>
        </>
      )}

      {read !== null && read.status === 'ready' && read.items.length === 0 && !read.partial && (
        <p className="documents-nav__note">{WORKBENCH_COPY.documentsEmpty}</p>
      )}

      {read !== null && read.items.length > 0 && (
        <ul className="documents-nav__list">
          {read.items.map((item) => (
            <li key={item.document_id}>
              <button
                type="button"
                className="documents-nav__row"
                aria-current={current === item.document_id ? 'true' : undefined}
                onClick={() => {
                  void openDocument({ documentId: item.document_id, title: item.title }, { gesture: true })
                  onNavigate?.()
                }}
              >
                <span className="documents-nav__title">{item.title}</span>
                <span className="documents-nav__type">{documentTypeRead(item.type).label}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {read !== null && (read.status === 'error' || read.partial) && (
        <div className="documents-nav__error">
          <p className="documents-nav__note">{WORKBENCH_COPY.documentsError}</p>
          <button type="button" className="documents-nav__retry" onClick={retry}>
            {WORKBENCH_COPY.documentsRetry}
          </button>
        </div>
      )}
    </section>
  )
}
