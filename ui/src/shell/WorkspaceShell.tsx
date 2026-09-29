/**
 * WorkspaceShell — the signed-in workspace: TopBar, AppHeader, LeftNav and
 * the mode-aware ChatPane.
 *
 * Wide (768px and up): a fixed 268px LeftNav beside a flexible main area.
 *
 * Narrow (below 768px — the interactions ADR's LAYOUT-3, bead
 * agent-forge-harness-0rn): one column. LeftNav becomes an off-canvas MODAL
 * drawer behind TopBar's "Open navigation" (LAYOUT-10): focus moves into it,
 * Tab wraps inside it, Escape and the scrim close it, focus returns to the
 * menu button, and the chrome and the chat are `inert` behind it. ModelPicker
 * and the theme control move from AppHeader into the drawer.
 *
 * Layout rules this component keeps:
 * - The drawer host is rendered at EVERY layout, so LeftNav never remounts on
 *   a layout change and a rename in progress keeps its draft (LAYOUT-6).
 *   `<main>` never remounts either, so the composer's draft survives too.
 * - Exactly one ModelPicker is mounted at every layout, whether or not the
 *   drawer is open: it loads the /models catalog ChatPane sends by.
 * - The drawer state lives in React memory only — no storage, URL or history
 *   entry — and every mount starts closed (X-7: conversation titles can hold
 *   GM prep text, so on a phone at the table they stay behind a closed drawer).
 * - Every layout decision reads a `satisfies Record<ShellLayout, …>` table,
 *   so adding a layout (1kg.6.3's 'medium') is a compile error here rather
 *   than a silent fallthrough that mounts no picker.
 */

import * as React from 'react'
import { AppHeader } from './AppHeader'
import { useShellLayout, type ShellLayout } from './breakpoints'
import { ChatPane } from './ChatPane'
import { wrapTab } from './focusTrap'
import { LeftNav } from './LeftNav'
import { ModelCatalogProvider } from './ModelCatalogContext'
import { NavSettings } from './NavSettings'
import { TopBar, type TopBarNavToggle } from './TopBar'
import './WorkspaceShell.css'

const NAVIGATION = 'Navigation'
const CLOSE_NAVIGATION = 'Close navigation'

/** Whether LeftNav lives in a drawer behind TopBar's menu button. */
const HAS_DRAWER = { narrow: true, wide: false } as const satisfies Record<ShellLayout, boolean>

/** Where ModelPicker and the theme control live: exactly one place per layout. */
const SETTINGS_HOME = { narrow: 'drawer', wide: 'header' } as const satisfies Record<
  ShellLayout,
  'drawer' | 'header'
>

export function WorkspaceShell(): React.JSX.Element {
  const layout = useShellLayout()
  const hasDrawer: boolean = HAS_DRAWER[layout]
  const settingsInDrawer = SETTINGS_HOME[layout] === 'drawer'

  const [open, setOpen] = React.useState(false)
  // Widening closes the drawer. Adjusted during render (the
  // CustomiseRailDialog pattern), never by a setState inside an effect, so a
  // later narrowing starts closed rather than springing the drawer back open.
  if (!hasDrawer && open) setOpen(false)
  const drawerOpen = hasDrawer && open

  const navId = React.useId()
  const navHostRef = React.useRef<HTMLDivElement>(null)
  const mainRef = React.useRef<HTMLElement>(null)
  const menuButtonRef = React.useRef<HTMLButtonElement>(null)
  const lastFocusedRef = React.useRef<Element | null>(null)

  // Idempotent setters: opening an open drawer or closing a closed one
  // changes nothing, so neither moves focus.
  const openDrawer = React.useCallback(() => setOpen(true), [])
  const closeDrawer = React.useCallback(() => setOpen(false), [])

  // Focus into the drawer on open (its name is announced first), and back to
  // the menu button on an open -> closed transition at the narrow layout
  // only. A close caused by widening, the first mount, and StrictMode's
  // double effect run all move nothing.
  const wasOpenRef = React.useRef(false)
  React.useEffect(() => {
    const wasOpen = wasOpenRef.current
    wasOpenRef.current = drawerOpen
    if (drawerOpen && !wasOpen) {
      navHostRef.current?.focus()
    } else if (!drawerOpen && wasOpen && hasDrawer) {
      menuButtonRef.current?.focus()
    }
  }, [drawerOpen, hasDrawer])

  // LAYOUT-6: a layout change moves focus only if the focused element ceased
  // to exist, and then to the visible view — `<main>`, as the workspace has
  // no heading. Two ways it can cease to exist: it is inside the drawer host
  // that just became hidden, or it unmounted (the model and theme controls
  // move between AppHeader and the drawer) and focus fell to <body>.
  const layoutRef = React.useRef(layout)
  React.useEffect(() => {
    if (layoutRef.current === layout) return
    layoutRef.current = layout
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
  }, [layout, hasDrawer, drawerOpen])

  // Escape closes the drawer from anywhere in the document — including after
  // a rename ends and focus falls to <body>, where the host's own keydown
  // never fires. React's root listener runs first on the bubble path, so an
  // Escape an inner surface already handled (the user menu, the rename input)
  // arrives here `defaultPrevented`, and the innermost surface closes alone.
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
  }

  const navToggle: TopBarNavToggle | undefined = hasDrawer
    ? { expanded: drawerOpen, controls: navId, onOpen: openDrawer, buttonRef: menuButtonRef }
    : undefined

  // agent-forge-harness-bta: the one ModelPicker (AppHeader's or the
  // drawer's) loads the /models catalog and ChatPane sends by it, so both
  // read the one copy held here.
  return (
    <ModelCatalogProvider>
      <div className="workspace-shell" data-layout={layout} onFocus={recordFocus}>
        <div className="workspace-shell__chrome" inert={drawerOpen}>
          <TopBar navToggle={navToggle} />
          <AppHeader showSettings={!settingsInDrawer} />
        </div>

        <div className="workspace-shell__body">
          <div
            id={navId}
            ref={navHostRef}
            className="workspace-shell__nav"
            data-open={drawerOpen || undefined}
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

          {drawerOpen && (
            <div className="workspace-shell__scrim" aria-hidden="true" onClick={closeDrawer} />
          )}

          <main ref={mainRef} className="workspace-shell__main" tabIndex={-1} inert={drawerOpen}>
            <ChatPane />
          </main>
        </div>
      </div>
    </ModelCatalogProvider>
  )
}
