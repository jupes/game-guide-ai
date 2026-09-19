/**
 * VersionList — a document's history (agent-forge-harness-1kg.6.1, CANVAS-27).
 *
 * Newest first, 20 to a page behind **Load more**, each row naming its label,
 * summary, author (`You` / `Assistant`), time and changed fields. The current
 * version is marked, and every older one offers **Restore**.
 *
 * Restore is **additive** (CANVAS-26): it appends a new version whose content
 * equals the chosen one, so nothing is lost and the record asks for no
 * confirmation dialog beyond the loss guard the owner already runs (CANVAS-16).
 * The list is controlled: it fetches nothing and decides nothing.
 *
 * ## Differences from the handoff's `VersionList` in `CanvasPane.d.ts`
 *
 * | Handoff prop | Here | Why |
 * | --- | --- | --- |
 * | `versions: DocumentVersion[]` with `{ id, label, summary, author, time }` | `versions: readonly DocumentVersion[]` from `gm/contracts` | the wire's version is `{ number, author, summary, created_at, sealed, changed_fields, restored_from }`; `label` and a display `time` are derived here (CANVAS-27) |
 * | `time: string` — `'7:36 PM'` | `created_at` ISO, formatted client-side | CANVAS-27 rejects pre-formatted times |
 * | `activeId?: string` | `currentVersionNumber: number \| null` | a version's identity on the wire is its number; the id was the handoff's own |
 * | — | `status`, `hasMore`, `loadingMore`, `onLoadMore`, `onRetry` | §12.2's empty / loading / error rows and CANVAS-27's paging |
 * | — | `restoringVersionNumber`, `documentType`, `locale`, `labelledBy`, `id` | in-flight restore, field labels from the registry, locale-aware times, and the disclosure's labelling |
 *
 * Privacy (X-7): summaries and titles are GM-private. They are rendered and
 * nothing else — never an id, a class, a data attribute or a stored value.
 */

import * as React from 'react'
import { Button } from '../ds/Button'
import type { DocumentVersion } from './contracts'
import { authorLabel, fieldLabel, formatTimestamp, versionLabel } from './canvasStatus'
import './VersionList.css'

/** §12.2's three history states. */
export type VersionListStatus = 'ready' | 'loading' | 'error'

export interface VersionListProps {
  /** Newest first (CANVAS-27). Rendered in the order given; never re-sorted. */
  versions?: readonly DocumentVersion[]
  /** The version the canvas is showing. Its row is marked current. */
  currentVersionNumber?: number | null
  /** @default 'ready' */
  status?: VersionListStatus
  /** The server has another page (CANVAS-27 pages {@link HISTORY_PAGE_SIZE}). */
  hasMore?: boolean
  /** A further page is in flight — **Load more** shows inline progress (§12.2). */
  loadingMore?: boolean
  onLoadMore?: () => void
  /** §12.2: `Couldn't load history` is never a dead end. */
  onRetry?: () => void
  /** CANVAS-26. Omitted, no row offers a restore. */
  onRestore?: (version: DocumentVersion) => void
  /** The version whose restore is in flight, if any. */
  restoringVersionNumber?: number | null
  /** Document type id — the registry's field labels for `changed_fields` (X-8). */
  documentType?: string
  /** BCP-47 tag(s) for the client-side time format. Defaults to the reader's. */
  locale?: string | string[]
  /** Id of the element naming this list, e.g. the disclosure's heading. */
  labelledBy?: string
  id?: string
  className?: string
  style?: React.CSSProperties
}

const SKELETON_ROWS = 3

export function VersionList({
  versions = [],
  currentVersionNumber = null,
  status = 'ready',
  hasMore = false,
  loadingMore = false,
  onLoadMore,
  onRetry,
  onRestore,
  restoringVersionNumber = null,
  documentType,
  locale,
  labelledBy,
  id,
  className,
  style,
}: VersionListProps): React.JSX.Element {
  const listId = React.useId()
  const rowRefs = React.useRef(new Map<number, HTMLLIElement>())
  const loadMoreRef = React.useRef<HTMLButtonElement>(null)
  // How many rows were on screen when Load more was pressed, so that focus can
  // land on the first row that arrived once the button itself has gone.
  const awaitingPage = React.useRef<number | null>(null)

  React.useEffect(() => {
    const pending = awaitingPage.current
    if (pending === null || loadingMore || versions.length <= pending) return
    awaitingPage.current = null
    // The button survives another page: leave focus where the reader put it.
    if (hasMore && loadMoreRef.current !== null) return
    const arrived = versions[pending]
    if (arrived === undefined) return
    rowRefs.current.get(arrived.number)?.focus()
  }, [versions, loadingMore, hasMore])

  function handleLoadMore(): void {
    awaitingPage.current = versions.length
    onLoadMore?.()
  }

  const classes = ['gm-versions', className].filter(Boolean).join(' ')
  const statusId = `${listId}-status`

  if (status === 'error') {
    return (
      <div className={classes} id={id} style={style} data-status="error">
        <p className="gm-versions__message" role="alert">
          Couldn&apos;t load history
        </p>
        {onRetry !== undefined && (
          <Button variant="text" size="small" icon="refresh" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    )
  }

  if (status === 'loading') {
    return (
      <div className={classes} id={id} style={style} data-status="loading">
        <p className="gm-versions__message" role="status">
          Loading version history…
        </p>
        <div className="gm-versions__skeleton" aria-hidden="true">
          {Array.from({ length: SKELETON_ROWS }, (_, row) => (
            <span className="gm-versions__skeleton-row" key={row} />
          ))}
        </div>
      </div>
    )
  }

  if (versions.length === 0) {
    return (
      <div className={classes} id={id} style={style} data-status="empty">
        <p className="gm-versions__message">Only one version so far</p>
      </div>
    )
  }

  return (
    <div className={classes} id={id} style={style} data-status="ready">
      {/* STATE-7 rations announcements: one when an operation starts, one when
          it ends. Only a restore or a further page speaks here. */}
      <p className="gm-versions__live" id={statusId} role="status">
        {restoringVersionNumber !== null
          ? `Restoring ${versionLabel(restoringVersionNumber)}…`
          : loadingMore
            ? 'Loading older versions…'
            : ''}
      </p>

      <ul className="gm-versions__list" aria-labelledby={labelledBy}>
        {versions.map((version) => {
          const current = version.number === currentVersionNumber
          const restoring = version.number === restoringVersionNumber
          const label = versionLabel(version.number)
          const formatted = formatTimestamp(version.created_at, locale)
          return (
            <li
              className="gm-versions__row"
              key={version.number}
              aria-current={current ? 'true' : undefined}
              data-current={current ? 'true' : undefined}
              tabIndex={-1}
              ref={(node) => {
                if (node === null) rowRefs.current.delete(version.number)
                else rowRefs.current.set(version.number, node)
              }}
            >
              <span className="gm-versions__label">{label}</span>
              <div className="gm-versions__detail">
                <p className="gm-versions__summary">{version.summary}</p>
                <p className="gm-versions__meta">
                  <span className="gm-versions__author">{authorLabel(version.author)}</span>
                  {' · '}
                  {formatted === null ? (
                    <span>{version.created_at}</span>
                  ) : (
                    <time dateTime={version.created_at}>{formatted}</time>
                  )}
                  {current && (
                    <>
                      {' · '}
                      <span className="gm-versions__current">Current</span>
                    </>
                  )}
                  {version.restored_from !== null && (
                    <>
                      {' · '}
                      <span className="gm-versions__restored">
                        Restored from {versionLabel(version.restored_from)}
                      </span>
                    </>
                  )}
                </p>
                {version.changed_fields.length > 0 && (
                  <p className="gm-versions__fields">
                    <span className="gm-versions__fields-label">Changed:</span>{' '}
                    {version.changed_fields.map((key) => fieldLabel(key, documentType)).join(', ')}
                  </p>
                )}
              </div>
              {onRestore !== undefined && !current && (
                // The row whose restore is in flight stays enabled so that it
                // keeps the focus the reader put on it; it is labelled busy and
                // ignores further presses. Every other row is disabled, because
                // restores are one at a time.
                <Button
                  variant="text"
                  size="small"
                  icon={restoring ? 'hourglass_top' : 'undo'}
                  disabled={restoringVersionNumber !== null && !restoring}
                  onClick={() => {
                    if (restoringVersionNumber === null) onRestore(version)
                  }}
                >
                  {restoring ? `Restoring ${label}…` : `Restore ${label}`}
                </Button>
              )}
            </li>
          )
        })}
      </ul>

      {hasMore && (
        <button
          type="button"
          className="gm-versions__load-more"
          ref={loadMoreRef}
          disabled={loadingMore}
          aria-describedby={statusId}
          onClick={handleLoadMore}
        >
          <span className="material-symbols-rounded" aria-hidden="true">
            {loadingMore ? 'progress_activity' : 'expand_more'}
          </span>
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
