/**
 * CanvasPane — the persistent canvas beside the chat (agent-forge-harness-1kg.6.1).
 *
 * It owns the document's control surface and nothing about the document's
 * contents: the body is `children`, which 1kg.6.2's renderers fill. It is a
 * controlled component — no fetches, no stores, no routing, no editing state.
 *
 * What it implements from `docs/adr/gm-workbench-interactions.md`:
 *
 * - **CANVAS-13** — one aggregate save status in one polite live region. The
 *   live region holds the message and nothing else, so a re-render that changes
 *   the title, the body or a control never re-announces the status.
 * - **CANVAS-27** — the version chip is a disclosure over `VersionList`.
 * - **CANVAS-29** — the gold wash is acknowledged with **Got it** in the header.
 * - **CANVAS-32** — closing is guarded and returns focus to the control that
 *   opened the canvas, or to the composer when that control is gone. Both
 *   arrive as refs: the pane never goes looking for them in the document.
 * - **REVEAL-13 / REVEAL-14** — the header repeats what the server's live
 *   projection says, as **two** controls (open the sheet, and a separate Stop),
 *   never the handoff's single pill where a stray click stopped the reveal. It
 *   can say `Reveal state unknown — reconnecting`, which a boolean cannot.
 * - **§10.2 canvas-header row** — `wide` shows everything; `compact` (below
 *   560 px of canvas width) hides the meta line and makes the reveal control an
 *   icon while **Stop showing** keeps its text; `fullScreen` (the narrow
 *   layout) replaces close with a back control and moves export and history
 *   into an overflow menu.
 * - **LAYOUT-9 / LAYOUT-10** — 44 px touch floor, visible focus, reduced motion
 *   honoured, both themes from tokens; the overflow menu closes on Escape and
 *   returns focus to its trigger without trapping it.
 *
 * Left to later beads: the document renderers and `SelectionBar` (1kg.6.2), the
 * shell and the persistent canvas state (1kg.6.3), editing, AI edits and
 * conflict resolution (1kg.6.5), the reveal sheet (1kg.7.3) and the export menu
 * (1kg.6.6). This pane exposes the actions; it does not perform them.
 *
 * ## Differences from the handoff's `CanvasPaneProps` (`CanvasPane.d.ts`)
 *
 * | Handoff prop | Here | Why |
 * | --- | --- | --- |
 * | `typeLabel?: string` | `documentType: string` | the label and icon come from the registry; an unknown type renders a neutral placeholder and never NPC (X-8) |
 * | `icon?: string` (default `'description'`) | — | same: the registry owns the icon |
 * | `accent?: string` | — | a raw colour prop; every colour here is a token, in both themes |
 * | `meta?: string` — `'edited 7:36 PM'` | `updatedAt?: string \| null` (ISO) | CANVAS-27 rejects pre-formatted times; this client formats, locale-aware, with a machine-readable `<time dateTime>` |
 * | `version?: string` | `versionNumber?: number \| null` | a version's identity on the wire is its number; `v3` is derived |
 * | `onVersions?: () => void` | `historyOpen` + `onToggleHistory` + `history` | a disclosure needs controlled state to carry `aria-expanded` / `aria-controls` |
 * | `revealed?: boolean` + `revealSummary?: string` | `reveal: CanvasReveal` | REVEAL-13 drops `data.revealed`; the state is the server's live projection and has an *unknown* case, which must never read as "nothing revealed" |
 * | `onClose?: () => void` | `onRequestClose` (vetoable) + `openerRef` + `composerRef` | CANVAS-16's loss guard may refuse; CANVAS-32 fixes where focus lands |
 * | — | `saveStatus`, `onRetrySave` | CANVAS-13 |
 * | — | `layout` | §10.2's three canvas-header presentations |
 * | — | `changeHighlight`, `onAcknowledgeChange` | CANVAS-28 / CANVAS-29 |
 * | — | `onBack` | §10.2: the narrow layout's back control |
 * | — | `locale` | client-side time formatting |
 * | `children`, `style` | unchanged | |
 *
 * Privacy (X-7): the title, the version summaries and any field text are
 * GM-private. They are rendered and nothing more — never an id, a class, a data
 * attribute, a URL or a stored value. Every id this pane needs comes from
 * `useId`.
 */

import * as React from 'react'
import { Badge } from '../ds/Badge'
import { Button } from '../ds/Button'
import { Chip } from '../ds/Chip'
import { IconButton } from '../ds/IconButton'
import { VersionList, type VersionListProps } from './VersionList'
import {
  type CanvasReveal,
  type CanvasSaveStatus,
  canvasRevealRead,
  canvasStatusRead,
  documentTypeRead,
  formatTimestamp,
  versionLabel,
} from './canvasStatus'
import './CanvasPane.css'

/**
 * §10.2's three canvas-header presentations. `compact` follows the *canvas*
 * width (below 560 px), not the viewport, so 1kg.6.3 — which owns the shell and
 * therefore the measurement — passes it in rather than this pane guessing.
 */
export type CanvasLayout = 'wide' | 'compact' | 'fullScreen'

export interface CanvasPaneProps {
  /** The document's name. GM-private (X-7). */
  title: string
  /** A `DocumentTypeId` from the contract. Unknown renders neutrally (X-8). */
  documentType: string
  /** When the document was last saved, as an ISO timestamp. */
  updatedAt?: string | null
  /** The current version's number; renders the history disclosure when set. */
  versionNumber?: number | null
  /** CANVAS-13's aggregate. @default 'saved' */
  saveStatus?: CanvasSaveStatus
  /** Offered while `saveStatus` is `error` (STATE-2). */
  onRetrySave?: () => void
  /** REVEAL-13: from the server's live projection. @default { state: 'hidden' } */
  reveal?: CanvasReveal
  /** Opens this document's reveal sheet (1kg.7.3). Omitted, no reveal control. */
  onReveal?: () => void
  /** REVEAL-6: immediate, no dialog, idempotent. Omitted, no Stop control. */
  onStopReveal?: () => void
  /** EXPORT-1: opens the export menu (1kg.6.6). It never downloads by itself. */
  onExport?: () => void
  /** CANVAS-27's disclosure state. @default false */
  historyOpen?: boolean
  onToggleHistory?: () => void
  /** Forwarded to {@link VersionList} while the disclosure is open. */
  history?: VersionListProps
  /** CANVAS-28: a change arrived live in this client and is washed gold. */
  changeHighlight?: boolean
  /** CANVAS-29's **Got it**. */
  onAcknowledgeChange?: () => void
  /**
   * CANVAS-16 + CANVAS-32. The owner runs the loss guard and returns `false` to
   * keep the canvas open; anything else closes it and focus returns.
   */
  onRequestClose?: () => boolean | void | Promise<boolean | void>
  /** §10.2: the narrow layout's back control. Falls back to a guarded close. */
  onBack?: () => void
  /** CANVAS-32: the control that opened the canvas. Focus returns here. */
  openerRef?: React.RefObject<HTMLElement | null>
  /** CANVAS-32: the composer, used when the opener is gone. */
  composerRef?: React.RefObject<HTMLElement | null>
  /** @default 'wide' */
  layout?: CanvasLayout
  /** BCP-47 tag(s) for the client-side time format. Defaults to the reader's. */
  locale?: string | string[]
  /** The document body — 1kg.6.2's renderers. */
  children?: React.ReactNode
  className?: string
  style?: React.CSSProperties
}

export function CanvasPane({
  title,
  documentType,
  updatedAt = null,
  versionNumber = null,
  saveStatus = 'saved',
  onRetrySave,
  reveal = { state: 'hidden' },
  onReveal,
  onStopReveal,
  onExport,
  historyOpen = false,
  onToggleHistory,
  history,
  changeHighlight = false,
  onAcknowledgeChange,
  onRequestClose,
  onBack,
  openerRef,
  composerRef,
  layout = 'wide',
  locale,
  children,
  className,
  style,
}: CanvasPaneProps): React.JSX.Element {
  const ids = React.useId()
  const titleId = `${ids}-title`
  const historyId = `${ids}-history`
  const historyHeadingId = `${ids}-history-heading`
  const menuId = `${ids}-menu`

  const [menuOpen, setMenuOpen] = React.useState(false)
  const menuTriggerRef = React.useRef<HTMLButtonElement>(null)
  const menuRef = React.useRef<HTMLDivElement>(null)

  const type = documentTypeRead(documentType)
  const statusRead = canvasStatusRead(saveStatus, onRetrySave !== undefined)
  const revealRead = canvasRevealRead(reveal)
  const editedAt = updatedAt === null ? null : formatTimestamp(updatedAt, locale)
  const full = layout === 'fullScreen'
  const compact = layout === 'compact'
  const inMenu = full && (onExport !== undefined || (versionNumber !== null && onToggleHistory !== undefined))

  // ── CANVAS-32 ──────────────────────────────────────────────────────────────
  // Both targets arrive as refs. A control that has been removed from the
  // document is "gone"; the composer then takes the focus. Nothing here queries
  // the document for an element.
  const returnFocus = React.useCallback((): void => {
    const opener = openerRef?.current ?? null
    const target = opener !== null && opener.isConnected ? opener : (composerRef?.current ?? null)
    target?.focus()
  }, [openerRef, composerRef])

  const requestClose = React.useCallback(async (): Promise<void> => {
    if (onRequestClose === undefined) return
    const outcome = await onRequestClose()
    if (outcome === false) return
    returnFocus()
  }, [onRequestClose, returnFocus])

  // ── Overflow menu (§10.2, narrow) ──────────────────────────────────────────
  const closeMenu = React.useCallback((focusTrigger: boolean): void => {
    setMenuOpen(false)
    if (focusTrigger) menuTriggerRef.current?.focus()
  }, [])

  React.useEffect(() => {
    if (!menuOpen) return
    // LAYOUT-10: a non-modal surface never traps focus — it closes instead.
    function onPointerDown(event: MouseEvent): void {
      const target = event.target
      if (!(target instanceof Node)) return
      if (menuRef.current?.contains(target) === true) return
      if (menuTriggerRef.current?.contains(target) === true) return
      setMenuOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [menuOpen])

  React.useEffect(() => {
    if (!menuOpen) return
    const first = menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')
    first?.focus()
  }, [menuOpen])

  function onMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    // A closed menu handles nothing, so Escape still reaches whatever owns the
    // canvas rather than being swallowed by a menu that is not there.
    if (!menuOpen) return
    if (event.key === 'Escape') {
      event.stopPropagation()
      closeMenu(true)
      return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
    if (items.length === 0) return
    const at = items.findIndex((item) => item === document.activeElement)
    const next = event.key === 'ArrowDown' ? at + 1 : at - 1
    items[(next + items.length) % items.length]?.focus()
  }

  const historyDisclosure =
    versionNumber !== null && onToggleHistory !== undefined ? (
      <Chip
        key="history"
        label={versionLabel(versionNumber)}
        icon="history"
        onClick={onToggleHistory}
        className="gm-canvas__version"
        aria-expanded={historyOpen}
        aria-controls={historyId}
        aria-label={`${versionLabel(versionNumber)} — version history`}
      />
    ) : null

  const exportControl =
    onExport === undefined ? null : <IconButton key="export" icon="download" ariaLabel="Export" onClick={onExport} />

  return (
    <section
      className={['gm-canvas', className].filter(Boolean).join(' ')}
      aria-labelledby={titleId}
      data-layout={layout}
      style={style}
    >
      <header className="gm-canvas__header">
        <span className="material-symbols-rounded gm-canvas__type-icon" aria-hidden="true">
          {type.icon}
        </span>

        <div className="gm-canvas__identity">
          <h2 className="gm-canvas__title" id={titleId}>
            {title}
          </h2>
          {/* §10.2: below 560 px of canvas width the meta line hides. It stays
              in the accessibility tree so that the type is never lost — the icon
              beside it is decorative. */}
          <p className={`gm-canvas__meta${compact ? ' gm-canvas__meta--offscreen' : ''}`}>
            <span className="gm-canvas__type-label">{type.label}</span>
            {updatedAt !== null && (
              <>
                {' · '}
                {editedAt === null ? (
                  <span>Edited {updatedAt}</span>
                ) : (
                  <span>
                    Edited <time dateTime={updatedAt}>{editedAt}</time>
                  </span>
                )}
              </>
            )}
          </p>
        </div>

        {/* CANVAS-13: ONE polite live region, holding the message and nothing
            else. React writes this text node only when the string itself
            changes, so an unrelated re-render is silent. */}
        <p className="gm-canvas__status" role="status" aria-live="polite" data-tone={statusRead.tone}>
          {statusRead.message}
        </p>
        {saveStatus === 'error' && onRetrySave !== undefined && (
          <Button variant="text" size="small" icon="refresh" onClick={onRetrySave}>
            Retry
          </Button>
        )}

        {/* CANVAS-29: the gold wash is acknowledged here. */}
        {changeHighlight && onAcknowledgeChange !== undefined && (
          <span className="gm-canvas__wash">
            <Badge tone="gold">New changes are highlighted</Badge>
            <Button variant="text" size="small" onClick={onAcknowledgeChange}>
              Got it
            </Button>
          </span>
        )}

        {/* REVEAL-13 / REVEAL-14 — state, then two separate controls. */}
        {revealRead.message !== null && (
          <span className="gm-canvas__reveal-state" data-state={reveal.state}>
            <span className="gm-canvas__reveal-message">{revealRead.message}</span>
            {revealRead.note !== null && <span className="gm-canvas__reveal-note">{revealRead.note}</span>}
          </span>
        )}
        {revealRead.openLabel !== null &&
          onReveal !== undefined &&
          (compact ? (
            <IconButton icon={revealRead.openIcon} ariaLabel={revealRead.openLabel} onClick={onReveal} />
          ) : (
            <Button variant="tonal" size="small" icon={revealRead.openIcon} onClick={onReveal}>
              {revealRead.openLabel}
            </Button>
          ))}
        {revealRead.stopLabel !== null && onStopReveal !== undefined && (
          // §10.2: Stop showing keeps its text on every layout.
          <Button variant="outlined" size="small" icon="visibility_off" onClick={onStopReveal}>
            {revealRead.stopLabel}
          </Button>
        )}

        {!full && historyDisclosure}
        {!full && exportControl}

        {inMenu && (
          <div className="gm-canvas__menu-anchor" onKeyDown={onMenuKeyDown}>
            <button
              type="button"
              className="gm-canvas__more"
              ref={menuTriggerRef}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-controls={menuId}
              aria-label="More canvas actions"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <span className="material-symbols-rounded" aria-hidden="true">
                more_vert
              </span>
            </button>
            {menuOpen && (
              <div className="gm-canvas__menu" role="menu" id={menuId} ref={menuRef} aria-label="Canvas actions">
                {versionNumber !== null && onToggleHistory !== undefined && (
                  <button
                    type="button"
                    role="menuitem"
                    className="gm-canvas__menu-item"
                    aria-expanded={historyOpen}
                    aria-controls={historyId}
                    onClick={() => {
                      onToggleHistory()
                      closeMenu(true)
                    }}
                  >
                    <span className="material-symbols-rounded" aria-hidden="true">
                      history
                    </span>
                    Version history
                  </button>
                )}
                {onExport !== undefined && (
                  <button
                    type="button"
                    role="menuitem"
                    className="gm-canvas__menu-item"
                    // EXPORT-1: Export opens a menu, never a one-click download.
                    aria-haspopup="menu"
                    onClick={() => {
                      onExport()
                      closeMenu(true)
                    }}
                  >
                    <span className="material-symbols-rounded" aria-hidden="true">
                      download
                    </span>
                    Export
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {full ? (
          <Button
            variant="text"
            size="small"
            icon="arrow_back"
            className="gm-canvas__back"
            onClick={() => {
              if (onBack !== undefined) onBack()
              else void requestClose()
            }}
          >
            Back
          </Button>
        ) : (
          onRequestClose !== undefined && (
            <IconButton icon="close" ariaLabel="Close canvas" onClick={() => void requestClose()} />
          )
        )}
      </header>

      {/* Rendered whether or not it is open so that `aria-controls` always
          resolves; `hidden` keeps the closed panel out of the a11y tree, and
          the list itself mounts only while the disclosure is open. */}
      {versionNumber !== null && (
        <section
          className="gm-canvas__history"
          id={historyId}
          aria-labelledby={historyHeadingId}
          hidden={!historyOpen}
        >
          <h3 className="gm-canvas__history-heading" id={historyHeadingId}>
            Version history
          </h3>
          {historyOpen && (
            <VersionList
              {...(history ?? {})}
              currentVersionNumber={history?.currentVersionNumber ?? versionNumber}
              documentType={history?.documentType ?? documentType}
              locale={history?.locale ?? locale}
              labelledBy={historyHeadingId}
            />
          )}
        </section>
      )}

      {/* A scrolling region with no focusable content of its own would be
          unreachable from the keyboard, so it is a tab stop. */}
      <div className="gm-canvas__body" data-wash={changeHighlight ? 'true' : undefined} tabIndex={0}>
        {children}
      </div>
    </section>
  )
}
