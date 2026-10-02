/**
 * WorkspaceShell — the signed-in workspace: TopBar, AppHeader, LeftNav, the
 * mode-aware ChatPane and, for a GM with a campaign, the Workbench canvas.
 *
 * Three layouts (interactions ADR LAYOUT-1..3; `breakpoints.ts` owns the numbers):
 *
 * - Wide (1024px and up): a fixed 268px LeftNav beside the chat. While a document is
 *   open the sidebar steps aside for a 56px rail (the canvas needs its width) and
 *   the chat and the canvas sit side by side (chat 472px, canvas the rest).
 * - Medium (768 to 1023px, bead agent-forge-harness-1kg.6.3): one column and always
 *   the 56px rail. LeftNav is the same off-canvas drawer, opened from the rail.
 * - Narrow (below 768px — LAYOUT-3, bead agent-forge-harness-0rn): one column and
 *   no rail; the drawer opens from TopBar's "Open navigation".
 *
 * Wherever LeftNav is in the drawer it is a MODAL one (LAYOUT-10): focus moves into
 * it, Tab wraps inside it, Escape and the scrim close it, focus returns to the
 * button that opened it, and the chrome, the rail and the chat are `inert` behind
 * it. ModelPicker and the theme control move from AppHeader into the drawer on the
 * narrow layout only.
 *
 * The Workbench (1kg.6.3): a canvas column beside, or instead of, the chat, driven by
 * `canvasContext`. It exists only for a `dm` in the GM channel with a campaign
 * selected (X-9); in Sage, Spell and Rules it is hidden, not closed. Below 1024px a
 * Chat / Canvas switch (a row in the chrome) picks which single column shows.
 *
 * Layout rules this component keeps:
 * - The drawer host is rendered at EVERY layout, so LeftNav never remounts on a
 *   layout change and a rename in progress keeps its draft (LAYOUT-6). `<main>`
 *   never remounts either, and ChatPane is one memoized element at one tree
 *   position in every state, so the composer's draft and a turn in flight survive
 *   opening, closing and switching the canvas and crossing a breakpoint.
 * - Exactly one ModelPicker is mounted at every layout, whether or not the
 *   drawer is open: it loads the /models catalog ChatPane sends by.
 * - The drawer state lives in React memory only — no storage, URL or history
 *   entry — and every mount starts closed (X-7: conversation titles can hold
 *   GM prep text, so on a phone at the table they stay behind a closed drawer).
 * - Every layout decision reads a `satisfies Record<ShellLayout, …>` table, so
 *   adding a layout is a compile error here rather than a silent fallthrough that
 *   mounts no picker.
 * - The loss-guard dialog, the reveal sheet (1kg.7.3) and the Workbench's announcers
 *   render at the root, as siblings of the chrome and the body, outside every
 *   container this component makes `inert`.
 */

import * as React from 'react'
import { CanvasHost } from '../gm/CanvasHost'
import { RevealSheetHost } from '../gm/RevealSheetHost'
import { AppHeader } from './AppHeader'
import { useShellLayout, type ShellLayout } from './breakpoints'
import { canvasShown, CanvasProvider, useCanvasActions, useCanvasState, useWorkbenchActive } from './canvasContext'
import { ChatPane } from './ChatPane'
import { wrapTab } from './focusTrap'
import { LeftNav } from './LeftNav'
import { LossGuardHost } from './LossGuardDialog'
import { ModelCatalogProvider } from './ModelCatalogContext'
import { NavRail } from './NavRail'
import { NavSettings } from './NavSettings'
import { RevealAnnouncer } from './RevealAnnouncer'
import { RevealProvider, useRevealSheetOpen } from './revealContext'
import { TableSessionProvider } from './tableSession'
import { TopBar, type TopBarNavToggle } from './TopBar'
import { WorkbenchAnnouncer } from './WorkbenchAnnouncer'
import { DOCUMENTS_HEADING_ID, WORKBENCH_COPY } from './workbenchCopy'
import { WorkbenchViewSwitch } from './WorkbenchViewSwitch'
import './WorkspaceShell.css'

const NAVIGATION = 'Navigation'
const CLOSE_NAVIGATION = 'Close navigation'

/** How LeftNav is presented: the fixed sidebar, a 56px rail with the drawer behind it, or the drawer behind TopBar's menu. */
type NavPresentation = 'sidebar' | 'rail' | 'hidden'

/** By layout, and by whether the canvas is SHOWN (a document open in an active Workbench). */
const NAV_PRESENTATION = {
  narrow: { plain: 'hidden', canvas: 'hidden' },
  medium: { plain: 'rail', canvas: 'rail' },
  wide: { plain: 'sidebar', canvas: 'rail' },
} as const satisfies Record<ShellLayout, Record<'plain' | 'canvas', NavPresentation>>

/** Where ModelPicker and the theme control live: exactly one place per layout. */
const SETTINGS_HOME = { narrow: 'drawer', medium: 'header', wide: 'header' } as const satisfies Record<
  ShellLayout,
  'drawer' | 'header'
>

type Column = 'chat' | 'canvas'

export function WorkspaceShell(): React.JSX.Element {
  // agent-forge-harness-bta: the one ModelPicker (AppHeader's or the drawer's)
  // loads the /models catalog and ChatPane sends by it, so both read the one
  // copy held here.
  return (
    <ModelCatalogProvider>
      <TableSessionProvider>
        <RevealProvider>
          <CanvasProvider>
            <WorkspaceShellBody />
          </CanvasProvider>
        </RevealProvider>
      </TableSessionProvider>
    </ModelCatalogProvider>
  )
}

function WorkspaceShellBody(): React.JSX.Element {
  const layout = useShellLayout()
  const workbench = useWorkbenchActive()
  const { doc, view, guardDialog } = useCanvasState()
  const { setView, titleRef, chatRegionRef, setDrawerOpener } = useCanvasActions()
  const shown = canvasShown(workbench, doc)
  const presentation: NavPresentation = NAV_PRESENTATION[layout][shown ? 'canvas' : 'plain']
  const hasDrawer = presentation !== 'sidebar'
  const single = layout !== 'wide'
  const settingsInDrawer = SETTINGS_HOME[layout] === 'drawer'
  // The reveal sheet is modal too (1kg.7.3): the chrome and the body go `inert` while it is open for the open document.
  const sheetOpen = useRevealSheetOpen()
  const modal = guardDialog !== null || sheetOpen
  const chatVisible = !(single && shown && view === 'canvas')
  const canvasVisible = shown && (!single || view === 'canvas')

  const [open, setOpen] = React.useState(false)
  // The drawer closes when nothing needs it any more (widening to a sidebar). Adjusted
  // during render (the CustomiseRailDialog pattern), never by a setState inside an
  // effect, so a later narrowing starts closed rather than springing the drawer back open.
  if (!hasDrawer && open) setOpen(false)
  const drawerOpen = hasDrawer && open

  const navId = React.useId()
  const navHostRef = React.useRef<HTMLDivElement>(null)
  const mainRef = React.useRef<HTMLElement>(null)
  const chatColumnRef = React.useRef<HTMLDivElement>(null)
  const canvasColumnRef = React.useRef<HTMLDivElement>(null)
  const menuButtonRef = React.useRef<HTMLButtonElement>(null)
  const railButtonRef = React.useRef<HTMLButtonElement>(null)
  const lastFocusedRef = React.useRef<Element | null>(null)
  const focusColumnRef = React.useRef<Column | null>(null)
  const refocusRef = React.useRef<Element | null>(null)
  /** The rail's Campaign documents button opened the drawer: land on the documents heading. */
  const toDocumentsRef = React.useRef(false)
  // The button that opens the drawer is the rail's where the rail is, TopBar's otherwise.
  const openerRef = presentation === 'rail' ? railButtonRef : menuButtonRef

  // Opening an open drawer changes nothing, so it moves no focus; closing a closed
  // one likewise. The opener is told to the canvas, so a document opened from a row
  // in the drawer returns focus to it on close (C-3a).
  const openDrawer = React.useCallback(() => {
    setDrawerOpener(openerRef.current)
    setOpen(true)
  }, [openerRef, setDrawerOpener])
  const closeDrawer = React.useCallback(() => setOpen(false), [])
  const openDocuments = React.useCallback(() => {
    toDocumentsRef.current = true
    openDrawer()
  }, [openDrawer])

  // Focus into the drawer on open (its name is announced first), and back to the
  // button that opened it on an open -> closed transition while a drawer is still the
  // way to reach LeftNav. A close caused by widening to a sidebar, the first mount,
  // and StrictMode's double effect run all move nothing.
  const wasOpenRef = React.useRef(false)
  React.useEffect(() => {
    const wasOpen = wasOpenRef.current
    wasOpenRef.current = drawerOpen
    if (drawerOpen && !wasOpen) {
      const toDocuments = toDocumentsRef.current
      toDocumentsRef.current = false
      // The drawer's name is announced first; the documents button then lands on its list.
      if (toDocuments) document.getElementById(DOCUMENTS_HEADING_ID)?.focus()
      if (!toDocuments || document.activeElement === document.body) navHostRef.current?.focus()
    } else if (!drawerOpen && wasOpen && hasDrawer) {
      openerRef.current?.focus()
    }
  }, [drawerOpen, hasDrawer, openerRef])

  // LAYOUT-6: a layout change moves focus only if the focused element ceased to
  // exist, and then to the visible view — `<main>`, as the workspace has no heading.
  // Two ways it can cease to exist: it is inside the drawer host that just became
  // hidden, or it unmounted (the model and theme controls move between AppHeader and
  // the drawer) and focus fell to <body>.
  //
  // C-5: crossing from wide into a single column shows the column that holds focus,
  // so focus never lands in a column about to vanish; with focus in neither, the view
  // is kept.
  const layoutRef = React.useRef(layout)
  React.useEffect(() => {
    const previous = layoutRef.current
    if (previous === layout) return
    layoutRef.current = layout
    if (previous === 'wide' && layout !== 'wide' && shown) {
      const active = document.activeElement
      const column =
        active !== null && active !== document.body
          ? columnOf(active, chatColumnRef.current, canvasColumnRef.current)
          : focusColumnRef.current
      if (column !== null) {
        refocusRef.current = lastFocusedRef.current
        setView(column)
      }
    }
    const host = navHostRef.current
    const main = mainRef.current
    if (host === null || main === null) return
    const hostHidden = hasDrawer && !drawerOpen
    const active = document.activeElement
    const last = lastFocusedRef.current
    const hiddenWithFocus = hostHidden && active !== null && host.contains(active)
    const fellToBody =
      (active === null || active === document.body) &&
      last !== null &&
      (!last.isConnected || (hostHidden && host.contains(last)))
    if (hiddenWithFocus || fellToBody) main.focus()
  }, [layout, hasDrawer, drawerOpen, shown, setView])

  // After the view the crossing chose is on screen, an element the browser blurred
  // while its column was hidden gets focus back (a browser drops focus from a
  // `display: none` element; jsdom does not).
  React.useEffect(() => {
    const target = refocusRef.current
    if (!(target instanceof HTMLElement)) return
    refocusRef.current = null
    const restore = (): void => {
      const active = document.activeElement
      if (target.isConnected && (active === null || active === document.body)) target.focus()
    }
    restore()
    window.requestAnimationFrame(restore)
  }, [view, layout])

  // Escape closes the drawer from anywhere in the document — including after
  // a rename ends and focus falls to <body>, where the host's own keydown
  // never fires. React's root listener runs first on the bubble path, so an
  // Escape an inner surface already handled (the user menu, the rename input, the
  // loss-guard dialog) arrives here `defaultPrevented`, and the innermost surface
  // closes alone.
  React.useEffect(() => {
    if (!drawerOpen) return
    function handleEscape(event: KeyboardEvent): void {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      setOpen(false)
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [drawerOpen])

  function handleDrawerKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (navHostRef.current !== null) wrapTab(event, navHostRef.current)
  }

  function recordFocus(event: React.FocusEvent<HTMLDivElement>): void {
    lastFocusedRef.current = event.target
    focusColumnRef.current = columnOf(event.target, chatColumnRef.current, canvasColumnRef.current)
  }

  const navToggle: TopBarNavToggle | undefined =
    presentation === 'hidden'
      ? { expanded: drawerOpen, controls: navId, onOpen: openDrawer, buttonRef: menuButtonRef }
      : undefined

  // One element at one tree position in every state, so ChatPane never re-renders
  // for the canvas and never remounts across a layout change (LAYOUT-6, C-13).
  const chat = React.useMemo(() => <ChatPane />, [])

  return (
    <div className="workspace-shell" data-layout={layout} data-nav={presentation} onFocus={recordFocus}>
      {workbench && (
        <div
          className="workspace-shell__skip"
          role="group"
          aria-label={WORKBENCH_COPY.skipLinks}
          inert={drawerOpen || modal}
        >
          {chatVisible && (
            <button type="button" className="workspace-shell__skip-link" onClick={() => chatRegionRef.current?.focus()}>
              {WORKBENCH_COPY.skipToConversation}
            </button>
          )}
          {canvasVisible && (
            <button type="button" className="workspace-shell__skip-link" onClick={() => titleRef.current?.focus()}>
              {WORKBENCH_COPY.skipToDocument}
            </button>
          )}
        </div>
      )}

      <div className="workspace-shell__chrome" inert={drawerOpen || modal}>
        <TopBar navToggle={navToggle} />
        <AppHeader showSettings={!settingsInDrawer} />
        {single && shown && <WorkbenchViewSwitch view={view} onChange={setView} />}
      </div>

      <div className="workspace-shell__body" inert={modal}>
        {presentation === 'rail' && (
          <NavRail
            controls={navId}
            expanded={drawerOpen}
            onOpen={openDrawer}
            openButtonRef={railButtonRef}
            onOpenDocuments={workbench ? openDocuments : undefined}
            inert={drawerOpen}
          />
        )}

        <div
          id={navId}
          ref={navHostRef}
          className="workspace-shell__nav"
          data-open={drawerOpen || undefined}
          data-workbench-drawer={drawerOpen || undefined}
          role={drawerOpen ? 'dialog' : undefined}
          aria-modal={drawerOpen || undefined}
          aria-label={drawerOpen ? NAVIGATION : undefined}
          tabIndex={drawerOpen ? -1 : undefined}
          onKeyDown={drawerOpen ? handleDrawerKeyDown : undefined}
        >
          {drawerOpen && (
            <div className="workspace-shell__drawer-head">
              <button
                type="button"
                className="workspace-shell__drawer-close"
                aria-label={CLOSE_NAVIGATION}
                onClick={closeDrawer}
              >
                <span className="material-symbols-rounded" aria-hidden="true">
                  close
                </span>
              </button>
            </div>
          )}
          <LeftNav
            onNavigate={hasDrawer ? closeDrawer : undefined}
            settings={settingsInDrawer ? <NavSettings /> : undefined}
          />
        </div>

        {drawerOpen && <div className="workspace-shell__scrim" aria-hidden="true" onClick={closeDrawer} />}

        <main
          ref={mainRef}
          className="workspace-shell__main"
          tabIndex={-1}
          inert={drawerOpen}
          data-canvas={shown || undefined}
        >
          <div className="workbench" data-canvas={shown || undefined} data-view={view}>
            {/* Concealed, never `hidden` or inert, while the canvas shows in a single column:
                ChatPane's one status node must stay exposed (C-2). */}
            <div ref={chatColumnRef} className="workbench__chat" data-concealed={!chatVisible || undefined}>
              {chat}
            </div>
            {shown && (
              <div ref={canvasColumnRef} className="workbench__canvas" hidden={single && view === 'chat'}>
                <CanvasHost />
              </div>
            )}
          </div>
        </main>
      </div>

      {workbench && <WorkbenchAnnouncer />}
      {workbench && <RevealAnnouncer />}
      {workbench && <RevealSheetHost />}
      <LossGuardHost />
    </div>
  )
}

/** Which column of the Workbench holds `element`, or null for neither. */
function columnOf(element: Element, chat: HTMLElement | null, canvas: HTMLElement | null): Column | null {
  if (canvas?.contains(element) === true) return 'canvas'
  if (chat?.contains(element) === true) return 'chat'
  return null
}
