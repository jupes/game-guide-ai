/**
 * campaignContext -- the selected campaign, the campaign list and the scope
 * every campaign-scoped value is keyed by (agent-forge-harness-1kg.2.5, PR-1b;
 * brief sections 7.1 and 7.4-7.6 as amended by its Critic's items 3, 4, 6, 7,
 * 12, 14 and 15).
 *
 * Nothing about a campaign is kept anywhere but memory (SEC-49): no web
 * storage, and in the URL only the two opaque fragment keys `campaign` and
 * `conversation`, written by `workspaceFragment.ts` with `replaceState`.
 *
 * - Account-keyed. The state belongs to (user id, `canUseCampaigns(role)`):
 *   when either changes -- sign-in, sign-out, a centralized 401, an account
 *   switch, a role change -- it is replaced by a fresh one, every request in
 *   flight is aborted and every answer that arrives later is dropped. Guards
 *   are never consulted for that (I-8). Even the render in which the identity
 *   changes reads the fresh state (`snapshotFor`), never the previous
 *   account's.
 * - The server decides. A campaign id from a fragment, a hashchange or an
 *   object this provider did not list is only ever `restoring` until
 *   `GET /campaigns/{id}` answers; a `conversation` id is used only after
 *   `GET /conversations/{id}` says it is a live GM thread of that campaign
 *   (I-6). A URL can cause GET requests and nothing else.
 * - One restore per page load, decided at the FIRST settled auth status and
 *   never again (I-5): a page that settles signed out, or as an account that
 *   cannot use campaigns, strips the keys, returns to Sage and to Landing
 *   before anything paints, and a later sign-in never reads them.
 * - A campaign thread id never reaches a render outside its campaign's GM
 *   channel (critic 7): this provider re-provides `AppNavContext` with that id
 *   read as `null` wherever it does not belong, and sets the stored id to
 *   `null` on leaving the GM channel, a switch, a clear or an identity change
 *   (I-13).
 */

import * as React from 'react'
import { CampaignCreateRequestSchema, CONTRACT_VERSION, type Campaign } from '../gm/contracts'
import { AppNavContext, type AppNavState } from './AppNav'
import {
  createCampaign as postCampaign,
  getCampaign,
  getConversation,
  listCampaigns,
} from './campaignApi'
import { CurrentUserContext, type UserRole } from './currentUser'
import { pathForScreen } from './routes'
import {
  NO_WORKSPACE_KEYS,
  readWorkspaceKeys,
  replaceWorkspaceKeys,
  type CampaignRestore,
  type WorkspaceKeys,
} from './workspaceFragment'

// ── The public surface (brief section 7.1) ──────────────────────────────────

/** The client's courtesy mirror of the server's `dm` route gate (I-10, A-25):
 * the one place `ubw` changes when entitlement replaces it. For any other role
 * the campaign layer makes no request at all. */
// eslint-disable-next-line react-refresh/only-export-components -- the gate is co-located with its provider
export function canUseCampaigns(role: UserRole): boolean {
  return role === 'dm'
}

export type CampaignList =
  | { readonly kind: 'idle' }
  | { readonly kind: 'loading'; readonly items: readonly Campaign[] }
  | {
    readonly kind: 'ready'; readonly items: readonly Campaign[]; readonly nextCursor: string | null
    readonly loadingMore: boolean; readonly moreFailed: boolean
  }
  /** STATE-1: the items already shown stay. */
  | { readonly kind: 'failed'; readonly items: readonly Campaign[] }

export type CampaignSelection =
  | { readonly kind: 'none' }
  | { readonly kind: 'restoring'; readonly campaignId: string }
  | { readonly kind: 'selected'; readonly campaign: Campaign }
  /** 5xx or network: `retrySelection()` asks again. */
  | { readonly kind: 'failed'; readonly campaignId: string }
  /** 403, 404 or archived -- one state, carrying nothing (SEC-3), until the next switch. */
  | { readonly kind: 'unavailable' }

/** Non-null only while a campaign is `selected`. `key` is new on EVERY switch
 * and identity change -- A -> B -> A included -- so an answer requested under
 * an older key is always dropped (`isCurrentScope`). */
export interface CampaignScope {
  readonly campaignId: string
  readonly key: string
}

export type SwitchOutcome = 'switched' | 'unchanged' | 'vetoed' | 'unavailable' | 'failed'

/** LIB-25's loss guard. `campaignId: null` means leaving for no campaign, or for
 * a campaign not yet made (a create). Anything but `true` -- `false`, a
 * rejection, a throw -- vetoes, and nothing changes. */
export type SwitchGuard = (next: { readonly campaignId: string | null }) => boolean | Promise<boolean>

export type CreateOutcome =
  | { readonly kind: 'created'; readonly campaign: Campaign }
  /** A 422, or a name the contract refuses (no request is made). */
  | { readonly kind: 'invalid' }
  /** Anything else. Never retried by the client: a create has no idempotency key. */
  | { readonly kind: 'failed' }
  /** A guard said no, or another switch was already waiting on its guards. */
  | { readonly kind: 'vetoed' }

export interface CampaignContextValue {
  readonly enabled: boolean
  readonly list: CampaignList
  readonly selection: CampaignSelection
  readonly scope: CampaignScope | null
  /** Reads the first page: a no-op while `loading`; from `ready` or `failed` it
   * keeps the items on screen until the answer. Never called at mount. */
  loadCampaigns(): void
  /** A no-op without a cursor or while a page is already loading. */
  loadMoreCampaigns(): void
  /** No request for a campaign of the current list or the one just created;
   * any other object is re-read by id (`restoring` first). */
  selectCampaign(campaign: Campaign): Promise<SwitchOutcome>
  clearCampaign(): Promise<SwitchOutcome>
  /** Only in `failed`; a no-op while its own request is in flight. */
  retrySelection(): void
  /** Single-flight; guards run before the POST; on `created` the campaign
   * heads the list and is selected. */
  createCampaign(name: string, tone?: string | null): Promise<CreateOutcome>
  registerSwitchGuard(guard: SwitchGuard): () => void
  isCurrentScope(key: string): boolean
}

// ── Internal state ───────────────────────────────────────────────────────────

interface Snapshot {
  readonly account: string
  readonly enabled: boolean
  readonly list: CampaignList
  readonly selection: CampaignSelection
  readonly scope: CampaignScope | null
  /** A `conversation` id whose I-6 check has not finished: its key stays meanwhile. */
  readonly pendingThread: string | null
  /** Threads known to belong to `scope` (restored here; PR-2 adds created and listed ones). */
  readonly threads: readonly string[]
  /** Every campaign thread id seen on this page, whatever its scope or account. */
  readonly campaignThreads: readonly string[]
}

type Nav = Pick<AppNavState, 'screen' | 'mode' | 'conversationId' | 'setMode' | 'setConversationId' | 'backToLanding'>

const IDLE: CampaignList = { kind: 'idle' }
const NONE: CampaignSelection = { kind: 'none' }
const UNAVAILABLE: CampaignSelection = { kind: 'unavailable' }
const VETOED: CreateOutcome = { kind: 'vetoed' }
const FAILED: CreateOutcome = { kind: 'failed' }
const INVALID: CreateOutcome = { kind: 'invalid' }
const WORKSPACE_PATH = pathForScreen('workspace')
const NO_NAV: Nav = {
  screen: 'landing', mode: 'sage', conversationId: null,
  setMode: () => {}, setConversationId: () => {}, backToLanding: () => {},
}

function campaignIdOf(selection: CampaignSelection): string | null {
  if (selection.kind === 'selected') return selection.campaign.campaign_id
  if (selection.kind === 'restoring' || selection.kind === 'failed') return selection.campaignId
  return null
}

/** Whether the fragment names an own key at all, malformed or duplicated ones
 * included (names decoded as `workspaceFragment.ts` decodes them). */
function hasOwnPairs(hash: string): boolean {
  const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash)
  return params.has('campaign') || params.has('conversation')
}

function withThread(list: readonly string[], id: string): readonly string[] {
  return list.includes(id) ? list : [...list, id]
}

class CampaignStore {
  private snap: Snapshot
  private readonly listeners = new Set<() => void>()
  private readonly controllers = new Set<AbortController>()
  private guards: SwitchGuard[] = []
  private nav: Nav = NO_NAV
  private restore: CampaignRestore | null
  private readonly fetchImpl: typeof fetch | undefined
  private readonly win: Window
  private settled = false
  /** Bumped by every selection change and identity change: an answer requested
   * under an older value is dropped. */
  private selectionSeq = 0
  /** The same for the first-page read of the list. */
  private listSeq = 0
  /** Bumped by an identity change only. */
  private epoch = 0
  private scopeSeq = 0
  /** A switch waiting on its guards. */
  private busy: object | null = null
  private creating: Promise<CreateOutcome> | null = null
  private created: Campaign | null = null
  private transitional: { base: Snapshot; account: string; value: Snapshot } | null = null

  constructor(restore: CampaignRestore | null, fetchImpl: typeof fetch | undefined, win: Window, account: string) {
    this.restore = restore
    this.fetchImpl = fetchImpl
    this.win = win
    this.snap = this.fresh(account, false, [])
  }

  // ── useSyncExternalStore ──

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** The state for `account`: during the render in which the identity changed
   * (before `setIdentity` runs) a fresh state, never the previous account's. */
  snapshotFor(account: string, enabled: boolean): Snapshot {
    if (this.snap.account === account) return this.snap
    const cached = this.transitional
    if (cached !== null && cached.base === this.snap && cached.account === account) return cached.value
    const value = this.fresh(account, enabled, this.snap.campaignThreads)
    this.transitional = { base: this.snap, account, value }
    return value
  }

  private fresh(account: string, enabled: boolean, campaignThreads: readonly string[]): Snapshot {
    // The boot restore is still unconsumed only until the first settled status,
    // and `enabled` implies a settled one: this is exactly the restore about to start.
    const restore = enabled ? this.restore : null
    return {
      account,
      enabled,
      list: IDLE,
      selection: restore === null ? NONE : { kind: 'restoring', campaignId: restore.campaignId },
      scope: null,
      pendingThread: restore === null ? null : restore.conversationId,
      threads: [],
      campaignThreads,
    }
  }

  private set(next: Snapshot): void {
    this.snap = next
    for (const listener of [...this.listeners]) listener()
  }

  /** Change the selection. Every change drops the answers of the one before,
   * and a campaign conversation leaves with it (I-13). */
  private select(patch: Pick<Snapshot, 'selection' | 'scope' | 'pendingThread' | 'threads'>): number {
    this.selectionSeq += 1
    this.set({ ...this.snap, ...patch })
    this.dropCampaignConversation()
    return this.selectionSeq
  }

  private dropCampaignConversation(): void {
    const { conversationId } = this.nav
    if (conversationId !== null && this.snap.campaignThreads.includes(conversationId)) {
      this.nav.setConversationId(null)
    }
  }

  /** A fetch whose request is aborted by an identity change or an unmount. */
  private fetcher(): typeof fetch {
    const controller = new AbortController()
    this.controllers.add(controller)
    const base = this.fetchImpl ?? fetch
    return ((input: RequestInfo | URL, init?: RequestInit) => base(input, { ...init, signal: controller.signal })
      .finally(() => this.controllers.delete(controller))) as typeof fetch
  }

  // ── Wiring from the provider ──

  syncNav(nav: Nav): void {
    this.nav = nav
    if (nav.mode !== 'gm') this.dropCampaignConversation()
  }

  setIdentity(account: string, enabled: boolean, settled: boolean): void {
    const firstSettle = settled && !this.settled
    if (firstSettle) this.settled = true
    if (account !== this.snap.account) {
      this.epoch += 1
      this.selectionSeq += 1
      this.listSeq += 1
      this.busy = null
      this.creating = null
      this.created = null
      for (const controller of this.controllers) controller.abort()
      this.controllers.clear()
      this.set(this.fresh(account, enabled, this.snap.campaignThreads))
      this.dropCampaignConversation()
    }
    if (!firstSettle) return
    const restore = this.restore
    this.restore = null
    if (restore === null) return
    if (enabled) {
      void this.resolveById(restore.campaignId, restore.conversationId)
      return
    }
    // Abandoned (critic 6): signed out, or an account that cannot use
    // campaigns. Before paint -- this runs in a layout effect -- so Login and
    // Landing are never drawn with a key still in the address bar.
    replaceWorkspaceKeys(NO_WORKSPACE_KEYS, this.win)
    this.nav.setMode('sage')
    this.nav.backToLanding('replace')
  }

  dispose(): void {
    for (const controller of this.controllers) controller.abort()
    this.controllers.clear()
  }

  // ── The fragment (critic 3) ──

  private activeThread(): string | null {
    const { conversationId } = this.nav
    return conversationId !== null && this.snap.threads.includes(conversationId) ? conversationId : null
  }

  private desiredKeys(): WorkspaceKeys {
    const { screen, mode } = this.nav
    if (!this.snap.enabled || screen !== 'workspace' || mode !== 'gm') return NO_WORKSPACE_KEYS
    const campaignId = campaignIdOf(this.snap.selection)
    if (campaignId === null) return NO_WORKSPACE_KEYS
    return { campaignId, conversationId: this.snap.pendingThread ?? this.activeThread() }
  }

  /** Make the address bar say what the state says. Nothing is written before
   * the first settled status (a reload mid-check keeps the deep link), and a
   * fragment with no own key is never touched when none is wanted. */
  writeFragment(): void {
    if (!this.settled) return
    const keys = this.desiredKeys()
    if (keys.campaignId === null && !hasOwnPairs(this.win.location.hash)) return
    replaceWorkspaceKeys(keys, this.win)
  }

  /** CANVAS-30: a fragment changed outside the app is a switch gesture. */
  onHashChange(): void {
    const { hash, pathname } = this.win.location
    if (!this.settled || !this.snap.enabled) {
      if (hasOwnPairs(hash)) replaceWorkspaceKeys(NO_WORKSPACE_KEYS, this.win)
      return
    }
    if (pathname !== WORKSPACE_PATH) return
    const keys = readWorkspaceKeys(hash)
    const { selection, pendingThread } = this.snap
    if (keys.campaignId !== null && keys.campaignId !== campaignIdOf(selection)) {
      void this.switchFromLink(keys.campaignId, keys.conversationId)
      return
    }
    const thread = keys.conversationId
    if (thread !== null && selection.kind === 'selected' && thread !== pendingThread && thread !== this.activeThread()) {
      this.set({ ...this.snap, pendingThread: thread })
      void this.checkThread(thread)
      return
    }
    this.writeFragment()
  }

  private async switchFromLink(campaignId: string, thread: string | null): Promise<void> {
    if (this.creating !== null) {
      this.writeFragment()
      return
    }
    const outcome = await this.guarded({ campaignId }, () => {
      // A campaign link is a GM-channel link, as a cold load's is.
      this.nav.setMode('gm')
      return this.resolveById(campaignId, thread)
    })
    if (outcome === 'vetoed') this.writeFragment()
  }

  // ── Switching (section 7.6, critic 14) ──

  /** Run every guard in registration order; apply only if all said `true` and
   * nothing moved the selection or the identity meanwhile. */
  private async guarded(
    next: { readonly campaignId: string | null },
    apply: () => SwitchOutcome | Promise<SwitchOutcome>,
  ): Promise<SwitchOutcome> {
    if (this.busy !== null) return 'vetoed'
    const token = {}
    this.busy = token
    const seq = this.selectionSeq
    let pass = true
    for (const guard of [...this.guards]) {
      try {
        if ((await guard(next)) !== true) pass = false
      } catch {
        pass = false
      }
      if (!pass) break
    }
    if (this.busy === token) this.busy = null
    if (!pass || seq !== this.selectionSeq) return 'vetoed'
    return apply()
  }

  private applySelected(campaign: Campaign, thread: string | null): void {
    this.scopeSeq += 1
    this.select({
      selection: { kind: 'selected', campaign },
      scope: { campaignId: campaign.campaign_id, key: `scope-${this.scopeSeq}` },
      pendingThread: thread,
      threads: [],
    })
  }

  /** The server's say on an id from an untrusted source (section 7.4). */
  private async resolveById(campaignId: string, thread: string | null): Promise<SwitchOutcome> {
    const seq = this.select({ selection: { kind: 'restoring', campaignId }, scope: null, pendingThread: thread, threads: [] })
    const result = await getCampaign(campaignId, this.fetcher())
    if (seq !== this.selectionSeq) return 'vetoed'
    if (result.kind === 'ok') {
      this.applySelected(result.campaign, thread)
      if (thread !== null) void this.checkThread(thread)
      return 'switched'
    }
    if (result.kind === 'unavailable') {
      this.select({ selection: UNAVAILABLE, scope: null, pendingThread: null, threads: [] })
      return 'unavailable'
    }
    if (result.kind === 'failed') {
      this.select({ selection: { kind: 'failed', campaignId }, scope: null, pendingThread: thread, threads: [] })
    }
    return 'failed'
  }

  /** I-6: a thread id is used only once the server says it is a live GM thread
   * of the selected campaign; otherwise its key is dropped without a word. */
  private async checkThread(thread: string): Promise<void> {
    const seq = this.selectionSeq
    const result = await getConversation(thread, this.fetcher())
    const { selection, pendingThread } = this.snap
    if (seq !== this.selectionSeq || pendingThread !== thread || selection.kind !== 'selected') return
    const row = result.kind === 'ok' ? result.conversation : null
    const belongs = row !== null
      && row.campaign_id === selection.campaign.campaign_id
      && row.archived_at === null
      && (row.started_mode === 'gm' || row.started_mode === null)
      && this.nav.mode === 'gm'
    if (!belongs) {
      this.set({ ...this.snap, pendingThread: null })
      return
    }
    this.set({
      ...this.snap,
      pendingThread: null,
      threads: withThread(this.snap.threads, thread),
      campaignThreads: withThread(this.snap.campaignThreads, thread),
    })
    this.nav.setConversationId(thread)
  }

  private trusted(campaignId: string): Campaign | null {
    const { list } = this.snap
    const listed = list.kind === 'idle' ? undefined : list.items.find((c) => c.campaign_id === campaignId)
    if (listed !== undefined) return listed
    return this.created?.campaign_id === campaignId ? this.created : null
  }

  // ── The methods consumers call ──

  selectCampaign = (campaign: Campaign): Promise<SwitchOutcome> => {
    const { enabled, selection } = this.snap
    if (!enabled) return Promise.resolve('unchanged')
    if (selection.kind === 'selected' && selection.campaign.campaign_id === campaign.campaign_id) {
      return Promise.resolve('unchanged')
    }
    if (this.creating !== null) return Promise.resolve('vetoed')
    return this.guarded({ campaignId: campaign.campaign_id }, () => {
      const known = this.trusted(campaign.campaign_id)
      if (known === null) return this.resolveById(campaign.campaign_id, null)
      this.applySelected(known, null)
      return 'switched'
    })
  }

  clearCampaign = (): Promise<SwitchOutcome> => {
    const { enabled, selection } = this.snap
    if (!enabled || selection.kind === 'none') return Promise.resolve('unchanged')
    if (this.creating !== null) return Promise.resolve('vetoed')
    return this.guarded({ campaignId: null }, () => {
      this.select({ selection: NONE, scope: null, pendingThread: null, threads: [] })
      return 'switched'
    })
  }

  retrySelection = (): void => {
    const { enabled, selection, pendingThread } = this.snap
    if (enabled && selection.kind === 'failed') void this.resolveById(selection.campaignId, pendingThread)
  }

  loadCampaigns = (): void => {
    const { enabled, list } = this.snap
    if (enabled && list.kind !== 'loading') this.readFirstPage()
  }

  private readFirstPage(): void {
    const { list } = this.snap
    const seq = ++this.listSeq
    this.set({ ...this.snap, list: { kind: 'loading', items: list.kind === 'idle' ? [] : list.items } })
    void listCampaigns(null, this.fetcher()).then((result) => {
      if (seq !== this.listSeq) return
      const now = this.snap.list
      this.set({
        ...this.snap,
        list: result.kind === 'ok'
          ? { kind: 'ready', items: result.items, nextCursor: result.nextCursor, loadingMore: false, moreFailed: false }
          : { kind: 'failed', items: now.kind === 'idle' ? [] : now.items },
      })
    })
  }

  loadMoreCampaigns = (): void => {
    const { enabled, list } = this.snap
    if (!enabled || list.kind !== 'ready' || list.nextCursor === null || list.loadingMore) return
    const seq = this.listSeq
    this.set({ ...this.snap, list: { ...list, loadingMore: true, moreFailed: false } })
    void listCampaigns(list.nextCursor, this.fetcher()).then((result) => {
      const now = this.snap.list
      if (seq !== this.listSeq || now.kind !== 'ready') return
      if (result.kind !== 'ok') {
        this.set({ ...this.snap, list: { ...now, loadingMore: false, moreFailed: true } })
        return
      }
      const shown = new Set(now.items.map((c) => c.campaign_id))
      this.set({
        ...this.snap,
        list: {
          ...now,
          items: [...now.items, ...result.items.filter((c) => !shown.has(c.campaign_id))],
          nextCursor: result.nextCursor,
          loadingMore: false,
        },
      })
    })
  }

  createCampaign = (name: string, tone?: string | null): Promise<CreateOutcome> => {
    if (!this.snap.enabled) return Promise.resolve(FAILED)
    if (this.creating !== null) return this.creating
    if (this.busy !== null) return Promise.resolve(VETOED)
    // A name the contract refuses runs no guard and makes no request.
    const request = tone === undefined ? { schema_version: CONTRACT_VERSION, name } : { schema_version: CONTRACT_VERSION, name, tone }
    if (!CampaignCreateRequestSchema.safeParse(request).success) return Promise.resolve(INVALID)
    const run = this.create(name, tone)
    this.creating = run
    const done = (): void => {
      if (this.creating === run) this.creating = null
    }
    run.then(done, done)
    return run
  }

  private async create(name: string, tone: string | null | undefined): Promise<CreateOutcome> {
    const epoch = this.epoch
    // Guards before the POST (critic 15a): a veto after it would strand a made campaign.
    if ((await this.guarded({ campaignId: null }, () => 'switched')) === 'vetoed') return VETOED
    if (epoch !== this.epoch) return FAILED
    const result = await postCampaign(name, tone, this.fetcher())
    if (epoch !== this.epoch) return FAILED
    if (result.kind === 'created') {
      const { campaign } = result
      this.created = campaign
      const { list } = this.snap
      if (list.kind !== 'idle' && !list.items.some((c) => c.campaign_id === campaign.campaign_id)) {
        this.set({ ...this.snap, list: { ...list, items: [campaign, ...list.items] } })
      }
      this.applySelected(campaign, null)
      return { kind: 'created', campaign }
    }
    if (result.kind === 'invalid') return INVALID
    // Ambiguous: the server may have made it. One background re-read shows it
    // before the GM presses again (critic 15e) -- a GET, never a second POST.
    if (result.kind === 'failed') this.readFirstPage()
    return FAILED
  }

  registerSwitchGuard = (guard: SwitchGuard): (() => void) => {
    this.guards = [...this.guards, guard]
    return () => {
      this.guards = this.guards.filter((g) => g !== guard)
    }
  }

  isCurrentScope = (key: string): boolean => this.snap.scope?.key === key
}

/** The id AppNav consumers see: a campaign thread only in its own campaign's GM
 * channel, otherwise `null` (critic 7). */
function visibleConversation(nav: AppNavState, snap: Snapshot): string | null {
  const id = nav.conversationId
  if (id === null || !snap.campaignThreads.includes(id)) return id
  return nav.mode === 'gm' && snap.selection.kind === 'selected' && snap.threads.includes(id) ? id : null
}

// ── Context ──────────────────────────────────────────────────────────────────

function inertOutcome<T>(value: T): () => Promise<T> {
  return () => Promise.resolve(value)
}

const INERT: CampaignContextValue = {
  enabled: false,
  list: IDLE,
  selection: NONE,
  scope: null,
  loadCampaigns: () => {},
  loadMoreCampaigns: () => {},
  selectCampaign: inertOutcome<SwitchOutcome>('unchanged'),
  clearCampaign: inertOutcome<SwitchOutcome>('unchanged'),
  retrySelection: () => {},
  createCampaign: inertOutcome<CreateOutcome>(FAILED),
  registerSwitchGuard: () => () => {},
  isCurrentScope: () => false,
}

const CampaignContext = React.createContext<CampaignContextValue | null>(null)

export interface CampaignProviderProps {
  children: React.ReactNode
  /** The cold load's restore intent (`readCampaignRestore`), read once by `AppRoot`. */
  restore?: CampaignRestore | null
  fetchImpl?: typeof fetch
}

export function CampaignProvider({ children, restore = null, fetchImpl }: CampaignProviderProps): React.JSX.Element {
  // Read directly, not through useCurrentUser (which throws): standalone
  // mounts in tests and stories get a signed-out, disabled provider.
  const currentUser = React.useContext(CurrentUserContext)
  const nav = React.useContext(AppNavContext)
  const authStatus = currentUser?.authStatus ?? 'checking'
  const userId = currentUser?.user.id ?? 'guest'
  const enabled = authStatus === 'authenticated' && canUseCampaigns(currentUser?.user.role ?? 'player')
  const settled = authStatus === 'authenticated' || authStatus === 'unauthenticated'
  const account = `${enabled ? 'campaigns' : 'none'}:${userId}`
  const [store] = React.useState(() => new CampaignStore(restore, fetchImpl, window, account))
  const getSnapshot = React.useCallback(() => store.snapshotFor(account, enabled), [store, account, enabled])
  const snap = React.useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot)

  React.useLayoutEffect(() => {
    store.syncNav(nav)
  }, [store, nav])
  React.useLayoutEffect(() => {
    store.setIdentity(account, enabled, settled)
  }, [store, account, enabled, settled])
  // Passive, and in the parent of UrlNavigation: it runs after that
  // component's push, so leaving the workspace strips the keys on the NEW entry.
  React.useEffect(() => {
    store.writeFragment()
  }, [store, snap, nav.screen, nav.mode, nav.conversationId])
  React.useEffect(() => {
    const onHashChange = (): void => store.onHashChange()
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [store])
  React.useEffect(() => () => store.dispose(), [store])

  const value = React.useMemo<CampaignContextValue>(() => ({
    enabled: snap.enabled,
    list: snap.list,
    selection: snap.selection,
    scope: snap.scope,
    loadCampaigns: store.loadCampaigns,
    loadMoreCampaigns: store.loadMoreCampaigns,
    selectCampaign: store.selectCampaign,
    clearCampaign: store.clearCampaign,
    retrySelection: store.retrySelection,
    createCampaign: store.createCampaign,
    registerSwitchGuard: store.registerSwitchGuard,
    isCurrentScope: store.isCurrentScope,
  }), [snap, store])
  const visibleId = visibleConversation(nav, snap)
  const scopedNav = React.useMemo(
    () => (visibleId === nav.conversationId ? nav : { ...nav, conversationId: visibleId }),
    [nav, visibleId],
  )

  return (
    <AppNavContext.Provider value={scopedNav}>
      <CampaignContext.Provider value={value}>{children}</CampaignContext.Provider>
    </AppNavContext.Provider>
  )
}

/** The campaign context; outside a provider an inert, disabled value that
 * never fetches (so every existing test and story mounts unchanged). */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with provider
export function useCampaign(): CampaignContextValue {
  return React.useContext(CampaignContext) ?? INERT
}
