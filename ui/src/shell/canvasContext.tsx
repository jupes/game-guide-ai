/**
 * canvasContext -- the Workbench canvas's state: which document is open, which
 * column a single-column layout shows, and the loss guard around both
 * (agent-forge-harness-1kg.6.3; brief sections 2.2 to 2.5 and the Critic's C-3,
 * C-4, C-5, C-12, C-13 and C-17).
 *
 * - Who sees it (X-9): only a `dm` in the GM channel with a campaign `selected`
 *   (`useWorkbenchActive`). In Sage, Spell and Rules the canvas is HIDDEN, not
 *   closed: its state stays here and returns with the GM channel (CANVAS-9).
 * - Keyed per campaign scope (C-12). A campaign switch, a clear or an identity
 *   change replaces the whole state in the render it happens in, so no `Document`
 *   of a previous campaign or account stays in memory, and every answer that
 *   arrives later is dropped (LIB-25). The state is never written anywhere: the
 *   only trace outside memory is the opaque `document` fragment key, which the
 *   campaign store writes (X-7).
 * - No implicit swap or close (CANVAS-3/4/18). Nothing in chat calls
 *   `openDocument` or `closeDocument`. Only an `Open in canvas` link, a nav row, a
 *   hash link, the restore and Close/Back do.
 * - A gesture moves focus to the title once it renders, but only if focus has not
 *   moved since (C-4): a GM who started typing keeps the composer. A restore
 *   moves none.
 * - Two contexts (C-13). The ACTIONS are stable for the provider's life, so
 *   ChatPane and GmThread, which read only those, do not re-render when a
 *   document loads, opens or closes. The STATE changes on every transition.
 * - Nothing here rejects (C-17): an aborted request, a network failure and a
 *   throwing dirty source are all mapped to a state.
 *
 * Outside a provider every hook returns an inert value, so every existing test
 * and story mounts unchanged (the `useCampaign` precedent).
 */

import * as React from 'react'
import { flushSync } from 'react-dom'
import type { Document } from '../gm/contracts'
import { getDocument, type DocumentReadResult } from '../gm/documentApi'
import { documentTitle } from '../gm/documentTitle'
import { useAppNav } from './AppNav'
import { useCampaign, useCampaignDocument } from './campaignContext'
import { runGuard, type CanvasDirtySource } from './lossGuard'
import { WORKBENCH_COPY } from './workbenchCopy'

// ── The public surface ───────────────────────────────────────────────────────

export type CanvasDoc =
  | { readonly kind: 'closed' }
  | { readonly kind: 'loading'; readonly documentId: string; readonly title: string | null }
  | { readonly kind: 'open'; readonly document: Document }
  /** A newer schema or an unknown type (X-8). */
  | { readonly kind: 'unsupported'; readonly documentId: string }
  /** 403 and 404, one state that never says which (CANVAS-31). */
  | { readonly kind: 'unavailable'; readonly documentId: string }
  /** 5xx, a network failure, an unreadable body: worth a Retry. */
  | { readonly kind: 'failed'; readonly documentId: string; readonly title: string | null }

/** Which column a medium or narrow layout shows. Ignored at wide. */
export type WorkbenchView = 'chat' | 'canvas'

export interface GuardDialogState {
  /** The title of the document the guard is protecting. GM-private: shown, never stored. */
  readonly title: string
  readonly retryable: boolean
  /** A "Try saving again" is running. */
  readonly busy: boolean
}

export interface CanvasState {
  /** `closed` unless the stored scope key is the current one. */
  readonly doc: CanvasDoc
  readonly view: WorkbenchView
  /** Bumps when the nav list should refresh (a document was made or changed). */
  readonly documentsVersion: number
  /** Non-null while the loss-guard dialog is open: the shell makes everything else inert. */
  readonly guardDialog: GuardDialogState | null
  /** The one polite status line for canvas-level outcomes (`WorkbenchAnnouncer`). */
  readonly announcement: string
  /** Changes with every announcement, so an identical message is announced again. */
  readonly announcementTick: number
}

export interface OpenTarget {
  readonly documentId: string
  /** The title the link or row already shows, for the loading state. */
  readonly title?: string | null
}

export interface CanvasActions {
  /** Opens a document. A gesture (link, row, hash link) records the opener and moves
   * focus to the title; a restore moves none. Never rejects. */
  openDocument(target: OpenTarget, how?: { readonly gesture: boolean }): Promise<void>
  /** Guarded. Resolves `false` when the guard refused (the caller keeps focus where it is). Never rejects. */
  closeDocument(): Promise<boolean>
  /** From `failed`: asks again. */
  retry(): void
  setView(view: WorkbenchView): void
  /** 1kg.6.5's field editors register here; PR-1 registers none. */
  registerDirtySource(source: CanvasDirtySource): () => void
  bumpDocumentsVersion(): void
  /** The loss-guard dialog's three buttons (`LossGuardHost` is the only caller). */
  readonly guard: {
    readonly keep: () => void
    readonly retry: () => void
    readonly discard: () => void
  }
  readonly composerRef: React.RefObject<HTMLTextAreaElement | HTMLInputElement | null>
  /** Where focus returns when the canvas closes (CANVAS-32). */
  readonly openerRef: React.RefObject<HTMLElement | null>
  /** The canvas heading: the programmatic focus target of every state. */
  readonly titleRef: React.RefObject<HTMLHeadingElement | null>
  /** The `Conversation` region, which ChatPane sets so the shell never queries the DOM for it. */
  readonly chatRegionRef: React.RefObject<HTMLElement | null>
  /** How ChatPane hands over its `Conversation` region (a callback, so no ref is written from a component). */
  readonly setChatRegion: (element: HTMLElement | null) => void
  /** The control that opened the nav drawer; the shell sets it (C-3a). */
  readonly drawerOpenerRef: React.RefObject<HTMLElement | null>
  readonly setDrawerOpener: (element: HTMLElement | null) => void
}

/** The Workbench exists only for a `dm`, in the GM channel, with a campaign selected (X-9). */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useWorkbenchActive(): boolean {
  const { enabled, selection } = useCampaign()
  const { mode } = useAppNav()
  return enabled && mode === 'gm' && selection.kind === 'selected'
}

/** Whether the canvas column is shown: the Workbench is active and a document is open or opening. */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located with its provider
export function canvasShown(workbenchActive: boolean, doc: CanvasDoc): boolean {
  return workbenchActive && doc.kind !== 'closed'
}

// ── The store ────────────────────────────────────────────────────────────────

const CLOSED: CanvasDoc = { kind: 'closed' }
const NBSP = ' '

interface FocusRequest {
  /** What had focus at the gesture. */
  readonly opener: Element | null
}

interface Snapshot extends CanvasState {
  readonly scopeKey: string | null
  readonly focusRequest: FocusRequest | null
}

interface Refs {
  readonly composer: React.RefObject<HTMLTextAreaElement | HTMLInputElement | null>
  readonly opener: React.RefObject<HTMLElement | null>
  readonly title: React.RefObject<HTMLHeadingElement | null>
  readonly drawerOpener: React.RefObject<HTMLElement | null>
}

function fresh(scopeKey: string | null, documentsVersion: number, announcement: string, tick: number): Snapshot {
  return {
    scopeKey, doc: CLOSED, view: 'chat', documentsVersion, guardDialog: null, announcement,
    announcementTick: tick, focusRequest: null,
  }
}

function isHidden(element: Element): boolean {
  return element.isConnected && typeof element.checkVisibility === 'function' && !element.checkVisibility()
}

class CanvasStore {
  private snap: Snapshot = fresh(null, 0, '', 0)
  private readonly listeners = new Set<() => void>()
  private readonly refs: Refs
  private readonly fetchImpl: typeof fetch | undefined
  private transitional: { base: Snapshot; scopeKey: string | null; value: Snapshot } | null = null
  private campaignId: string | null = null
  private active = false
  private setDocumentKey: (id: string | null) => void = () => {}
  /** Bumped by every open, close and scope change: an answer requested under an older value is dropped. */
  private seq = 0
  private restoredScope: string | null = null
  private sources: readonly CanvasDirtySource[] = []
  private dialogResolve: ((proceed: boolean) => void) | null = null
  private listening = false

  constructor(refs: Refs, fetchImpl: typeof fetch | undefined) {
    this.refs = refs
    this.fetchImpl = fetchImpl
  }

  // ── useSyncExternalStore ──

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** The state for `scopeKey`: during the render in which the scope changed (before
   * `sync` runs) a fresh closed state, never the previous campaign's (C-12). */
  snapshotFor(scopeKey: string | null): Snapshot {
    if (this.snap.scopeKey === scopeKey) return this.snap
    const cached = this.transitional
    if (cached !== null && cached.base === this.snap && cached.scopeKey === scopeKey) return cached.value
    const value = fresh(scopeKey, this.snap.documentsVersion, '', this.snap.announcementTick)
    this.transitional = { base: this.snap, scopeKey, value }
    return value
  }

  private set(next: Snapshot): void {
    this.snap = next
    for (const listener of [...this.listeners]) listener()
  }

  // ── Wiring from the provider ──

  sync(scope: { key: string; campaignId: string } | null, active: boolean, setDocumentKey: (id: string | null) => void): void {
    this.active = active
    this.setDocumentKey = setDocumentKey
    this.campaignId = scope?.campaignId ?? null
    const key = scope?.key ?? null
    if (key === this.snap.scopeKey) return
    // C-12: replace the stored state, drop every answer in flight, forget the opener, and
    // let a waiting guard go (a refused switch is the safe answer to an identity change).
    this.seq += 1
    this.refs.opener.current = null
    const resolve = this.dialogResolve
    this.dialogResolve = null
    this.set(fresh(key, this.snap.documentsVersion, this.snap.announcement, this.snap.announcementTick))
    resolve?.(false)
  }

  mount(): void {
    this.attachUnload()
  }

  dispose(): void {
    this.detachUnload()
    const resolve = this.dialogResolve
    this.dialogResolve = null
    if (resolve !== null) {
      this.set({ ...this.snap, guardDialog: null })
      resolve(false)
    }
  }

  // ── The restore (CANVAS-31) ──

  /** Once per scope key, and only for an active Workbench: no document fetch in another channel. */
  restore(documentKey: string): void {
    const { scopeKey } = this.snap
    if (scopeKey === null || this.restoredScope === scopeKey) return
    this.restoredScope = scopeKey
    if (this.snap.doc.kind !== 'closed') return
    void this.openDocument({ documentId: documentKey }, { gesture: false })
  }

  /** A same-campaign fragment edit that names another document (CANVAS-30). */
  link(documentId: string): void {
    if (!this.active) return
    void this.openDocument({ documentId }, { gesture: true })
  }

  // ── Open ──

  openDocument = async (target: OpenTarget, how: { readonly gesture: boolean } = { gesture: false }): Promise<void> => {
    try {
      await this.open(target, how.gesture, true)
    } catch {
      // C-17: nothing here rejects; the unhandled-rejection guard in e2e fails a test that does.
    }
  }

  retry = (): void => {
    const { doc } = this.snap
    if (doc.kind !== 'failed') return
    // Focus follows the Retry press to the new heading, and the original opener is kept.
    void this.open({ documentId: doc.documentId, title: doc.title }, true, false).catch(() => {})
  }

  private captureOpener(): Element | null {
    const active = document.activeElement
    const drawerOpener = this.refs.drawerOpener.current
    // C-3a: a gesture from inside the open nav drawer returns to the control that opened the drawer.
    const opener =
      active instanceof Element && active.closest('[data-workbench-drawer]') !== null && drawerOpener?.isConnected === true
        ? drawerOpener
        : active instanceof HTMLElement
          ? active
          : null
    return opener
  }

  private isDirty(): boolean {
    return this.sources.some((source) => {
      try {
        return source.isDirty()
      } catch {
        return true
      }
    })
  }

  private async open(target: OpenTarget, gesture: boolean, recordOpener: boolean): Promise<void> {
    const { documentId } = target
    const title = target.title ?? null
    const { scopeKey } = this.snap
    const { campaignId } = this
    if (scopeKey === null || campaignId === null) return

    const opener = gesture ? this.captureOpener() : null
    if (gesture && recordOpener && opener instanceof HTMLElement) this.refs.opener.current = opener
    const focusRequest: FocusRequest | null = gesture ? { opener } : null

    const { doc } = this.snap
    const alreadyThere =
      (doc.kind === 'open' && doc.document.document_id === documentId) ||
      (doc.kind === 'loading' && doc.documentId === documentId)
    if (alreadyThere) {
      // CANVAS-5: the same document again fetches nothing; it shows the canvas and focuses its title.
      this.set({ ...this.snap, view: 'canvas', focusRequest: focusRequest ?? this.snap.focusRequest })
      return
    }

    const dirty = this.isDirty()
    const seq = ++this.seq
    if (!dirty) {
      this.set({ ...this.snap, doc: { kind: 'loading', documentId, title }, view: 'canvas', focusRequest })
      this.setDocumentKey(documentId)
    }

    const result = await getDocument(campaignId, documentId, this.fetchImpl)
    if (seq !== this.seq || this.snap.scopeKey !== scopeKey) return

    if (!dirty) {
      this.set({ ...this.snap, doc: this.docFor(result, documentId, title), focusRequest })
      return
    }
    // A dirty canvas keeps its document while the target loads: the guard never discards for
    // a destination that cannot load (§5.3).
    if (result.kind !== 'ok') {
      this.announce(WORKBENCH_COPY.openFailedNothingChanged(title))
      return
    }
    if (!(await this.guard())) return
    if (seq !== this.seq || this.snap.scopeKey !== scopeKey) return
    this.set({ ...this.snap, doc: this.docFor(result, documentId, title), view: 'canvas', focusRequest })
    this.setDocumentKey(documentId)
  }

  private docFor(result: DocumentReadResult, documentId: string, title: string | null): CanvasDoc {
    switch (result.kind) {
      case 'ok':
        return { kind: 'open', document: result.document }
      case 'unsupported':
        return { kind: 'unsupported', documentId }
      case 'unavailable':
        return { kind: 'unavailable', documentId }
      default:
        return { kind: 'failed', documentId, title }
    }
  }

  /** Runs after the commit that rendered the heading (the provider's effect). */
  applyFocus(): void {
    const request = this.snap.focusRequest
    if (request === null) return
    const { doc } = this.snap
    if (doc.kind === 'loading') return
    this.set({ ...this.snap, focusRequest: null })
    if (doc.kind === 'closed') return
    const active = document.activeElement
    const moved =
      active !== null && active !== document.body && active !== request.opener && !isHidden(active)
    const heading = this.refs.title.current
    if (!moved && heading !== null) {
      heading.focus()
      return
    }
    // Focus stayed where the GM put it, so a panel that is not an open document is read out instead.
    if (doc.kind === 'failed') this.announce(WORKBENCH_COPY.failedHeading(doc.title))
    else if (doc.kind === 'unavailable') this.announce(WORKBENCH_COPY.unavailableHeading)
    else if (doc.kind === 'unsupported') this.announce(WORKBENCH_COPY.unsupportedHeading)
  }

  // ── Close, view ──

  closeDocument = async (): Promise<boolean> => {
    try {
      if (this.snap.doc.kind === 'closed') return true
      if (!(await this.guard())) return false
      this.seq += 1
      // C-3b: applied before this resolves, so the caller's focus return lands on what re-appeared.
      flushSync(() => this.set({ ...this.snap, doc: CLOSED, view: 'chat', focusRequest: null }))
      this.setDocumentKey(null)
      return true
    } catch {
      return false
    }
  }

  setView = (view: WorkbenchView): void => {
    if (view !== this.snap.view) this.set({ ...this.snap, view })
  }

  bumpDocumentsVersion = (): void => {
    this.set({ ...this.snap, documentsVersion: this.snap.documentsVersion + 1 })
  }

  private announce(text: string): void {
    this.set({ ...this.snap, announcement: text, announcementTick: this.snap.announcementTick + 1 })
  }

  // ── The loss guard ──

  registerDirtySource = (source: CanvasDirtySource): (() => void) => {
    this.sources = [...this.sources, source]
    this.attachUnload()
    return () => {
      this.sources = this.sources.filter((candidate) => candidate !== source)
      if (this.sources.length === 0) this.detachUnload()
    }
  }

  private onBeforeUnload = (event: Event): void => {
    if (!this.isDirty()) return
    event.preventDefault()
    // Some browsers still require a returnValue to show the prompt.
    ;(event as BeforeUnloadEvent).returnValue = ''
  }

  private attachUnload(): void {
    if (this.listening || this.sources.length === 0) return
    window.addEventListener('beforeunload', this.onBeforeUnload)
    this.listening = true
  }

  private detachUnload(): void {
    if (!this.listening) return
    window.removeEventListener('beforeunload', this.onBeforeUnload)
    this.listening = false
  }

  private currentTitle(): string {
    const { doc } = this.snap
    if (doc.kind === 'open') return documentTitle(doc.document)
    if ((doc.kind === 'loading' || doc.kind === 'failed') && doc.title !== null) return doc.title
    return WORKBENCH_COPY.thisDocument
  }

  /** True to proceed, false to keep the document as it is. Never rejects. */
  guard = async (): Promise<boolean> => {
    try {
      if (this.dialogResolve !== null) return false
      const result = await runGuard(this.sources)
      if (result.kind === 'proceed') return true
      if (result.kind === 'refused-offline') {
        this.announce(WORKBENCH_COPY.offlineRefusal)
        return false
      }
      if (this.dialogResolve !== null) return false
      return await new Promise<boolean>((resolve) => {
        this.dialogResolve = resolve
        this.set({ ...this.snap, guardDialog: { title: this.currentTitle(), retryable: result.retryable, busy: false } })
      })
    } catch {
      return false
    }
  }

  private finishDialog(proceed: boolean): void {
    const resolve = this.dialogResolve
    if (resolve === null) return
    this.dialogResolve = null
    this.set({ ...this.snap, guardDialog: null })
    resolve(proceed)
  }

  guardKeep = (): void => {
    this.finishDialog(false)
  }

  guardDiscard = (): void => {
    if (this.snap.guardDialog?.busy === true) return
    for (const source of this.sources) {
      try {
        source.discard()
      } catch {
        // A source that cannot discard has nothing the dialog can lose further.
      }
    }
    this.finishDialog(true)
  }

  guardRetry = async (): Promise<void> => {
    const dialog = this.snap.guardDialog
    if (dialog === null || dialog.busy || this.dialogResolve === null) return
    this.set({ ...this.snap, guardDialog: { ...dialog, busy: true } })
    const result = await runGuard(this.sources)
    if (this.dialogResolve === null) return
    if (result.kind === 'proceed') {
      this.finishDialog(true)
      return
    }
    if (result.kind === 'refused-offline') this.announce(WORKBENCH_COPY.offlineRefusal)
    this.set({
      ...this.snap,
      guardDialog: { title: dialog.title, retryable: result.kind === 'ask' ? result.retryable : dialog.retryable, busy: false },
    })
  }
}

// ── Contexts ─────────────────────────────────────────────────────────────────

const INERT_STATE: CanvasState = {
  doc: CLOSED, view: 'chat', documentsVersion: 0, guardDialog: null, announcement: '', announcementTick: 0,
}

function inertRef<T>(): React.RefObject<T | null> {
  return { current: null }
}

const INERT_ACTIONS: CanvasActions = {
  openDocument: () => Promise.resolve(),
  closeDocument: () => Promise.resolve(true),
  retry: () => {},
  setView: () => {},
  registerDirtySource: () => () => {},
  bumpDocumentsVersion: () => {},
  guard: { keep: () => {}, retry: () => {}, discard: () => {} },
  composerRef: inertRef(),
  openerRef: inertRef(),
  titleRef: inertRef(),
  chatRegionRef: inertRef(),
  setChatRegion: () => {},
  drawerOpenerRef: inertRef(),
  setDrawerOpener: () => {},
}

const CanvasStateContext = React.createContext<CanvasState | null>(null)
const CanvasActionsContext = React.createContext<CanvasActions | null>(null)

export interface CanvasProviderProps {
  children: React.ReactNode
  fetchImpl?: typeof fetch
}

/**
 * The one provider for a Workbench. Nested inside another it passes its children
 * through: two stores would be two canvases, so a second mount (the shell inside a
 * test harness that already holds the provider, say) reuses the first.
 */
export function CanvasProvider({ children, fetchImpl }: CanvasProviderProps): React.JSX.Element {
  const parent = React.useContext(CanvasActionsContext)
  if (parent !== null) return <>{children}</>
  return <CanvasProviderRoot fetchImpl={fetchImpl}>{children}</CanvasProviderRoot>
}

function CanvasProviderRoot({ children, fetchImpl }: CanvasProviderProps): React.JSX.Element {
  const campaign = useCampaign()
  const { documentKey, setDocumentKey, onDocumentLink } = useCampaignDocument()
  const active = useWorkbenchActive()

  const composerRef = React.useRef<HTMLTextAreaElement | HTMLInputElement | null>(null)
  const openerRef = React.useRef<HTMLElement | null>(null)
  const titleRef = React.useRef<HTMLHeadingElement | null>(null)
  const chatRegionRef = React.useRef<HTMLElement | null>(null)
  const drawerOpenerRef = React.useRef<HTMLElement | null>(null)
  const setChatRegion = React.useCallback((element: HTMLElement | null): void => {
    chatRegionRef.current = element
  }, [])
  const setDrawerOpener = React.useCallback((element: HTMLElement | null): void => {
    drawerOpenerRef.current = element
  }, [])
  const [store] = React.useState(
    () => new CanvasStore({ composer: composerRef, opener: openerRef, title: titleRef, drawerOpener: drawerOpenerRef }, fetchImpl),
  )

  const scopeKey = campaign.scope?.key ?? null
  const campaignId = campaign.scope?.campaignId ?? null
  const getSnapshot = React.useCallback(() => store.snapshotFor(scopeKey), [store, scopeKey])
  const snap = React.useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot)

  React.useLayoutEffect(() => {
    store.sync(scopeKey === null || campaignId === null ? null : { key: scopeKey, campaignId }, active, setDocumentKey)
  }, [store, scopeKey, campaignId, active, setDocumentKey])
  React.useEffect(() => {
    store.mount()
    return () => store.dispose()
  }, [store])
  // CANVAS-31: a document named by the fragment opens once per scope, after the server has said the
  // campaign is the GM's, and only while the Workbench is active.
  React.useEffect(() => {
    if (active && documentKey !== null) store.restore(documentKey)
  }, [store, active, documentKey, scopeKey])
  // CANVAS-30: a fragment edit naming another document is a link.
  React.useEffect(() => onDocumentLink((documentId) => store.link(documentId)), [store, onDocumentLink])
  // LIB-25 / CANVAS-16: a campaign switch runs the loss guard; its boolean is the veto.
  const { registerSwitchGuard } = campaign
  React.useEffect(() => registerSwitchGuard(() => store.guard()), [store, registerSwitchGuard])
  // C-4: a gesture's focus move happens after the heading has rendered, and only if focus is still where the gesture left it.
  React.useEffect(() => {
    store.applyFocus()
  }, [store, snap.focusRequest, snap.doc])

  const actions = React.useMemo<CanvasActions>(
    () => ({
      openDocument: store.openDocument,
      closeDocument: store.closeDocument,
      retry: store.retry,
      setView: store.setView,
      registerDirtySource: store.registerDirtySource,
      bumpDocumentsVersion: store.bumpDocumentsVersion,
      guard: { keep: store.guardKeep, retry: () => void store.guardRetry(), discard: store.guardDiscard },
      composerRef,
      openerRef,
      titleRef,
      chatRegionRef,
      setChatRegion,
      drawerOpenerRef,
      setDrawerOpener,
    }),
    [store, setChatRegion, setDrawerOpener],
  )

  return (
    <CanvasActionsContext.Provider value={actions}>
      <CanvasStateContext.Provider value={snap}>{children}</CanvasStateContext.Provider>
    </CanvasActionsContext.Provider>
  )
}

/** The canvas state: re-renders on every transition. Chat code reads `useCanvasActions` instead. */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useCanvasState(): CanvasState {
  return React.useContext(CanvasStateContext) ?? INERT_STATE
}

/** Whether a canvas provider is mounted: the shell always has one; a bare LeftNav (a story, an old test) does not, and shows no documents list. */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useCanvasMounted(): boolean {
  return React.useContext(CanvasActionsContext) !== null
}

/** The canvas actions and refs: stable for the provider's life, so reading them never re-renders. */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useCanvasActions(): CanvasActions {
  return React.useContext(CanvasActionsContext) ?? INERT_ACTIONS
}

/** What `WorkbenchAnnouncer` renders: the text, alternating a trailing space so an identical message is read again. */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located with provider
export function announcementText(state: CanvasState): string {
  return state.announcement === '' ? '' : state.announcement + (state.announcementTick % 2 === 0 ? NBSP : '')
}
