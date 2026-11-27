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
import { cueCapability } from '../gm/cueApi'
import { useCampaign } from './campaignContext'
import { useWorkbenchActive } from './canvasContext'
import { useTableSession } from './tableSession'
import { LIBRARY_TABS, type LibraryCategoryId as DocumentCategoryId } from './useLibraryList'

export { LIBRARY_TABS }
export type LibraryCategoryId = DocumentCategoryId | 'cues'
const TABS_WITH_CUES: readonly LibraryCategoryId[] = [...LIBRARY_TABS, 'cues']

export const LIBRARY_PANEL_ID = 'workbench-library-panel'
export const LIBRARY_HEADING_ID = 'workbench-library-heading'
export const PARTICIPANTS_PANEL_ID = 'workbench-participants-panel'

export interface LibraryPanelState {
  readonly cueMediaEnabled: boolean
  readonly open: boolean
  readonly participantsOpen: boolean
  /** The tab shown while open, and the one the next open starts on. */
  readonly category: LibraryCategoryId
  readonly tabs: readonly LibraryCategoryId[]
}

export interface LibraryPanelActions {
  /** Opens at `category`, or switches to it when already open. `opener` is where focus returns on close. */
  openLibrary(category: LibraryCategoryId, opener: HTMLElement | null): void
  /** The rail icon: closes when open, else opens at the last category (the first time, NPCs). */
  toggleLibrary(opener: HTMLElement | null): void
  closeLibrary(how: { readonly returnFocus: boolean }): void
  setCategory(category: LibraryCategoryId): void
  toggleParticipants(opener: HTMLElement): void
}

type Stored = { readonly scopeKey: string } & (
  | { readonly kind: 'library'; readonly category: LibraryCategoryId }
  | { readonly kind: 'participants' }
)

const INERT: LibraryPanelState & LibraryPanelActions = {
  cueMediaEnabled: false,
  open: false,
  participantsOpen: false,
  category: 'npcs',
  tabs: LIBRARY_TABS,
  openLibrary: () => {},
  toggleLibrary: () => {},
  closeLibrary: () => {},
  setCategory: () => {},
  toggleParticipants: () => {},
}

const LibraryPanelContext = React.createContext<(LibraryPanelState & LibraryPanelActions) | null>(null)

export interface LibraryPanelProviderProps {
  children: React.ReactNode
  /** Where focus lands when the opener is gone or hidden: the rail's Campaign Library button, then `<main>`. */
  fallback?: {
    readonly rail: React.RefObject<HTMLElement | null>
    readonly participantsRail?: React.RefObject<HTMLElement | null>
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
  const { liveSession } = useTableSession()
  const checkCues = active || liveSession !== null
  const scopeKey = scope?.key ?? null
  const campaign = scope?.campaignId ?? null
  const [cueScope, setCueScope] = React.useState<string | null>(null)
  const cueMediaEnabled = scopeKey !== null && cueScope === scopeKey
  const tabs: readonly LibraryCategoryId[] = active && cueMediaEnabled ? TABS_WITH_CUES : LIBRARY_TABS
  React.useEffect(() => {
    if (!checkCues || campaign === null || scopeKey === null) return
    const campaignId = campaign
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let backoff = 1000
    let checking = false
    let settled = false
    async function check() {
      if (checking || settled || abort.signal.aborted) return
      clearTimeout(timer)
      checking = true
      const result = await cueCapability(campaignId, AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]), fetch)
      checking = false
      if (abort.signal.aborted) return
      if (result === 'unavailable') {
        timer = setTimeout(() => void check(), backoff)
        backoff = Math.min(30000, backoff * 2)
      } else {
        settled = true
        setCueScope(result === 'enabled' ? scopeKey : null)
      }
    }
    const onResume = () => { if (!document.hidden) void check() }
    void check()
    window.addEventListener('online', onResume)
    document.addEventListener('visibilitychange', onResume)
    return () => {
      abort.abort()
      clearTimeout(timer)
      window.removeEventListener('online', onResume)
      document.removeEventListener('visibilitychange', onResume)
    }
  }, [checkCues, campaign, scopeKey])
  const [stored, setStored] = React.useState<Stored | null>(null)
  /** The tab the next open starts on: the last one shown (the first time, NPCs). */
  const [last, setLast] = React.useState<LibraryCategoryId>('npcs')
  const openerRef = React.useRef<HTMLElement | null>(null)
  const returnFocusRef = React.useRef(false)

  // A switch, a clear, an identity change, or leaving the GM channel closes the panel in this very render,
  // and nothing reopens it on the way back (I-7).
  if (stored !== null && (!active || stored.scopeKey !== scopeKey)) setStored(null)
  const panel = active && stored?.scopeKey === scopeKey ? stored : null
  const open = panel?.kind === 'library'
  const participantsOpen = panel?.kind === 'participants'
  const selected = panel?.kind === 'library' ? panel.category : last
  const category = tabs.includes(selected) ? selected : 'npcs'

  const openLibrary = React.useCallback(
    (next: LibraryCategoryId, opener: HTMLElement | null): void => {
      if (!active || scopeKey === null) return
      if (opener !== null) openerRef.current = opener
      setLast(next)
      setStored({ scopeKey, kind: 'library', category: next })
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

  const toggleParticipants = React.useCallback((opener: HTMLElement): void => {
    if (!active || scopeKey === null) return
    if (participantsOpen) closeLibrary({ returnFocus: true })
    else {
      openerRef.current = opener
      setStored({ scopeKey, kind: 'participants' })
    }
  }, [active, closeLibrary, participantsOpen, scopeKey])

  const setCategory = React.useCallback((next: LibraryCategoryId): void => {
    setLast(next)
    setStored((now) => (now?.kind === 'library' ? { ...now, category: next } : now))
  }, [])

  // Focus returns once the panel has left the DOM, so a control it was covering is reachable again.
  const previousPanel = React.useRef<Stored['kind'] | null>(null)
  const kind = panel?.kind ?? null
  const rail = fallback?.rail
  const participantsRail = fallback?.participantsRail
  const main = fallback?.main
  React.useLayoutEffect(() => {
    const previous = previousPanel.current
    previousPanel.current = kind
    if (previous === null || kind !== null) return
    const wants = returnFocusRef.current
    returnFocusRef.current = false
    if (!wants) return
    const opener = openerRef.current
    const railButton = previous === 'participants' ? participantsRail?.current : rail?.current
    if (canTakeFocus(opener)) opener.focus()
    else if (canTakeFocus(railButton)) railButton.focus()
    else main?.current?.focus()
  }, [kind, rail, participantsRail, main])

  const value = React.useMemo(
    () => ({ open, participantsOpen, category, tabs, cueMediaEnabled, openLibrary, toggleLibrary, toggleParticipants, closeLibrary, setCategory }),
    [open, participantsOpen, category, tabs, cueMediaEnabled, openLibrary, toggleLibrary, toggleParticipants, closeLibrary, setCategory],
  )
  return <LibraryPanelContext.Provider value={value}>{children}</LibraryPanelContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useLibraryPanel(): LibraryPanelState & LibraryPanelActions {
  return React.useContext(LibraryPanelContext) ?? INERT
}
