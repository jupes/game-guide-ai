/**
 * libraryPanel -- whether the Campaign Library panel is open, and on which tab
 * (agent-forge-harness-1kg.6.4; brief 2.2; LIB-7 to LIB-10, LIB-25, X-7).
 *
 * - **Memory only** (X-7, INFERRED I-4). No URL, no storage, no history entry: a
 *   reload closes the panel, and the search text and titles inside it never leave
 *   the page.
 * - **Keyed by campaign scope** (LIB-25). The state names the scope key it was
 *   opened under, and `open` is true only while that is the current key AND the
 *   Workbench is active, so a campaign switch, a clear, an identity change or
 *   leaving the GM channel closes it in the render it happens in. Returning does
 *   not reopen it (INFERRED I-7). The adjustment is made while rendering, never by a
 *   setState inside an effect.
 * - **Focus** goes back to the control that opened the panel on every close except a
 *   pointer press in the chat column. If that control is gone or hidden (a row in a
 *   drawer that has since closed), to the rail's Campaign Library button, and failing
 *   that to `<main>`.
 * - Outside a provider every hook returns an inert value, so a bare LeftNav, a story
 *   or an old test mounts unchanged (the `canvasContext` precedent).
 */

import * as React from 'react'
import { useCampaign } from './campaignContext'
import { useWorkbenchActive } from './canvasContext'
import { LIBRARY_TABS, type LibraryCategoryId } from './useLibraryList'

export { LIBRARY_TABS }
export type { LibraryCategoryId }

export const LIBRARY_PANEL_ID = 'workbench-library-panel'
export const LIBRARY_HEADING_ID = 'workbench-library-heading'

export interface LibraryPanelState {
  readonly open: boolean
  /** The tab shown while open, and the one the next open starts on. */
  readonly category: LibraryCategoryId
}

export interface LibraryPanelActions {
  /** Opens at `category`, or switches to it when already open. `opener` is where focus returns on close. */
  openLibrary(category: LibraryCategoryId, opener: HTMLElement | null): void
  /** The rail icon: closes when open, else opens at the last category (the first time, NPCs). */
  toggleLibrary(opener: HTMLElement | null): void
  closeLibrary(how: { readonly returnFocus: boolean }): void
  setCategory(category: LibraryCategoryId): void
}

interface Stored {
  readonly scopeKey: string
  readonly category: LibraryCategoryId
}

const INERT: LibraryPanelState & LibraryPanelActions = {
  open: false,
  category: 'npcs',
  openLibrary: () => {},
  toggleLibrary: () => {},
  closeLibrary: () => {},
  setCategory: () => {},
}

const LibraryPanelContext = React.createContext<(LibraryPanelState & LibraryPanelActions) | null>(null)

export interface LibraryPanelProviderProps {
  children: React.ReactNode
  /** Where focus lands when the opener is gone or hidden: the rail's Campaign Library button, then `<main>`. */
  fallback?: {
    readonly rail: React.RefObject<HTMLElement | null>
    readonly main: React.RefObject<HTMLElement | null>
  }
}

/** Connected, and neither hidden by `display: none` nor inert: something a focus call can land on. */
function canTakeFocus(element: HTMLElement | null | undefined): element is HTMLElement {
  if (element === null || element === undefined || !element.isConnected) return false
  if (element.closest('[inert]') !== null) return false
  return !(typeof element.checkVisibility === 'function' && !element.checkVisibility())
}

export function LibraryPanelProvider({ children, fallback }: LibraryPanelProviderProps): React.JSX.Element {
  const { scope } = useCampaign()
  const active = useWorkbenchActive()
  const scopeKey = scope?.key ?? null
  const [stored, setStored] = React.useState<Stored | null>(null)
  /** The tab the next open starts on: the last one shown (the first time, NPCs). */
  const [last, setLast] = React.useState<LibraryCategoryId>('npcs')
  const openerRef = React.useRef<HTMLElement | null>(null)
  const returnFocusRef = React.useRef(false)

  // A switch, a clear, an identity change, or leaving the GM channel closes the panel in this very render,
  // and nothing reopens it on the way back (I-7).
  if (stored !== null && (!active || stored.scopeKey !== scopeKey)) setStored(null)
  const open = stored !== null && active && stored.scopeKey === scopeKey
  const category = stored?.category ?? last

  const openLibrary = React.useCallback(
    (next: LibraryCategoryId, opener: HTMLElement | null): void => {
      if (!active || scopeKey === null) return
      if (opener !== null) openerRef.current = opener
      setLast(next)
      setStored({ scopeKey, category: next })
    },
    [active, scopeKey],
  )

  const closeLibrary = React.useCallback((how: { readonly returnFocus: boolean }): void => {
    returnFocusRef.current = how.returnFocus
    setStored(null)
  }, [])

  const toggleLibrary = React.useCallback(
    (opener: HTMLElement | null): void => {
      if (open) closeLibrary({ returnFocus: true })
      else openLibrary(last, opener)
    },
    [open, last, closeLibrary, openLibrary],
  )

  const setCategory = React.useCallback((next: LibraryCategoryId): void => {
    setLast(next)
    setStored((now) => (now === null ? now : { ...now, category: next }))
  }, [])

  // Focus returns once the panel has left the DOM, so a control it was covering is reachable again.
  const wasOpen = React.useRef(false)
  const rail = fallback?.rail
  const main = fallback?.main
  React.useLayoutEffect(() => {
    const was = wasOpen.current
    wasOpen.current = open
    if (!was || open) return
    const wants = returnFocusRef.current
    returnFocusRef.current = false
    if (!wants) return
    const opener = openerRef.current
    const railButton = rail?.current
    if (canTakeFocus(opener)) opener.focus()
    else if (canTakeFocus(railButton)) railButton.focus()
    else main?.current?.focus()
  }, [open, rail, main])

  const value = React.useMemo(
    () => ({ open, category, openLibrary, toggleLibrary, closeLibrary, setCategory }),
    [open, category, openLibrary, toggleLibrary, closeLibrary, setCategory],
  )
  return <LibraryPanelContext.Provider value={value}>{children}</LibraryPanelContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useLibraryPanel(): LibraryPanelState & LibraryPanelActions {
  return React.useContext(LibraryPanelContext) ?? INERT
}
