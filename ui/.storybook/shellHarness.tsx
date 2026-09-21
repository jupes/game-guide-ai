/**
 * Story harness for the app shell (agent-forge-harness-27h).
 *
 * Every shell component reads at least one of four contexts — AppNav,
 * CurrentUser, ConversationStore, Theme — and several read three. Without a
 * harness each story file would build its own provider tower, they would drift,
 * and the temptation would be to add props to the components purely so they
 * could be rendered in isolation. Nothing here changes a component: the shell
 * is rendered through exactly the contexts `main.tsx` gives it.
 *
 * The state is REAL (`useState`-backed), not a frozen object, so an interaction
 * story can drive the shell the way a user does — pick a mode, open a
 * conversation, edit a display name — and see the consequence.
 *
 * It lives in `.storybook/` rather than `src/` on purpose: it is story
 * infrastructure, and `vite.config.ts` measures coverage over `src/**`, where a
 * non-story helper would be counted as untested product code.
 */

import * as React from 'react'
import type { Decorator } from '@storybook/react-vite'

import { ThemeProvider, type Theme } from '../src/ds/theme'
import { deriveInitials, type AvatarTone } from '../src/ds/Avatar'
import { AppNavContext, type AppNavState, type ChatMode, type Screen } from '../src/shell/AppNav'
import {
  CurrentUserContext,
  type AuthStatus,
  type CurrentUserContextValue,
  type UserRole,
} from '../src/shell/currentUser'
import { ConversationStoreContext } from '../src/shell/ConversationStoreContext'
import { MemoryConversationStore, type ConversationStore } from '../src/shell/conversationStore'

/** One conversation to put in the store before the story renders. */
export interface SeedConversation {
  mode: ChatMode
  /** Drives the derived title, and marks the conversation as started — which is
   * what makes ModelPicker treat a model change as a new conversation. */
  firstPrompt?: string
  /** An explicit rename, for the long-title and custom-title cases. */
  title?: string
  modelPreference?: string
}

export interface ShellHarnessOptions {
  screen?: Screen
  mode?: ChatMode
  /** Index into `conversations`. Omit for "no conversation open". */
  selected?: number
  conversations?: readonly SeedConversation[]
  role?: UserRole
  displayName?: string
  avatarTone?: AvatarTone
  authStatus?: AuthStatus
  /** Resolves false to exercise "the server refused to end the session". */
  signOut?: () => Promise<boolean>
  retryAuthCheck?: () => void
  /** Escape hatch for a story that needs to watch the store itself. */
  store?: ConversationStore
}

interface ShellHarnessProps extends ShellHarnessOptions {
  theme: Theme
  children: React.ReactNode
}

function seed(conversations: readonly SeedConversation[], store: ConversationStore): string[] {
  return conversations.map((row) => {
    const conversation = store.create(row.mode, row.firstPrompt, row.modelPreference)
    if (row.title !== undefined) store.rename(conversation.id, row.title)
    return conversation.id
  })
}

// eslint-disable-next-line react-refresh/only-export-components -- story-only module; it is never in the app bundle, so there is no Fast Refresh boundary to protect
function ShellHarness({
  theme,
  children,
  screen: initialScreen = 'workspace',
  mode: initialMode = 'sage',
  selected,
  conversations = [],
  role = 'dm',
  displayName: initialDisplayName = 'Alanna Quill',
  avatarTone: initialTone = 'gold',
  authStatus = 'authenticated',
  signOut = async () => true,
  retryAuthCheck = () => {},
  store: providedStore,
}: ShellHarnessProps): React.JSX.Element {
  // Built once per story mount, so a play function's edits survive re-renders
  // and do not leak into the next story.
  const [{ store, ids }] = React.useState(() => {
    const created = providedStore ?? new MemoryConversationStore()
    return { store: created, ids: providedStore ? [] : seed(conversations, created) }
  })

  const [screen, setScreen] = React.useState<Screen>(initialScreen)
  const [mode, setModeState] = React.useState<ChatMode>(initialMode)
  const [conversationId, setConversationIdState] = React.useState<string | null>(
    selected === undefined ? null : (ids[selected] ?? null),
  )

  // The callbacks are `useCallback`-stable exactly as AppNavProvider's are, and
  // that is load-bearing rather than tidiness: App re-runs "drop navigation
  // state when the identity changes" keyed on `backToLanding`'s identity, so a
  // fresh closure each render pins the app on the landing screen forever. A
  // harness that gets this wrong makes every navigation story impossible and
  // looks like a bug in the component.
  const enterWorkspace = React.useCallback((next: ChatMode = 'sage') => {
    setModeState(next)
    setScreen('workspace')
  }, [])
  const backToLanding = React.useCallback(() => setScreen('landing'), [])
  const openProfile = React.useCallback(() => setScreen('profile'), [])
  const backToWorkspace = React.useCallback(() => setScreen('workspace'), [])

  const nav = React.useMemo<AppNavState>(
    () => ({
      screen,
      mode,
      conversationId,
      enterWorkspace,
      setMode: setModeState,
      setConversationId: setConversationIdState,
      backToLanding,
      openProfile,
      backToWorkspace,
    }),
    [screen, mode, conversationId, enterWorkspace, backToLanding, openProfile, backToWorkspace],
  )

  const [displayName, setDisplayName] = React.useState(initialDisplayName)
  const [avatarTone, setAvatarTone] = React.useState<AvatarTone>(initialTone)

  const currentUser = React.useMemo<CurrentUserContextValue>(
    () => ({
      user: {
        id: 'story@aetheril.test',
        displayName,
        initials: deriveInitials(displayName),
        avatarTone,
        role,
        signOut,
        editProfile: () => {},
      },
      authStatus,
      retryAuthCheck,
      signIn: () => {},
      setDisplayName,
      setAvatarTone,
    }),
    [displayName, avatarTone, role, signOut, authStatus, retryAuthCheck],
  )

  return (
    <ThemeProvider initialTheme={theme}>
      <AppNavContext.Provider value={nav}>
        <CurrentUserContext.Provider value={currentUser}>
          <ConversationStoreContext.Provider value={store}>{children}</ConversationStoreContext.Provider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

/**
 * Wrap a story in the shell's contexts. The theme follows the toolbar global,
 * so the same story renders in Parchment and Tavern without a second decorator.
 */
export function withShell(options: ShellHarnessOptions = {}): Decorator {
  const Wrapped: Decorator = (Story, context) => (
    <ShellHarness {...options} theme={context.globals.theme === 'dark' ? 'dark' : 'light'}>
      <Story />
    </ShellHarness>
  )
  return Wrapped
}

/**
 * Replace `window.fetch` for the duration of one story, and put it back after.
 *
 * Login, Signup and the session check call the module-level `api` functions
 * with no injection point, so the network is the only seam. Returned as a CSF
 * `beforeEach` so the stub is installed before the story's first render — an
 * effect-based install lands after the session check has already gone out.
 */
export function stubFetch(
  handler: (url: string, init: RequestInit | undefined) => Response | Promise<Response>,
): () => () => void {
  return () => {
    const original = window.fetch
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve(handler(String(input), init))) as typeof fetch
    return () => {
      window.fetch = original
    }
  }
}

/** A JSON response, for `stubFetch` handlers. */
export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A request that never answers — the "still loading" state. */
export function pending(): Promise<Response> {
  return new Promise<Response>(() => {})
}
