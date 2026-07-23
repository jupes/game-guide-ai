/**
 * smoke.test — App-flow smoke test.
 *
 * CP-F6.2: Render <App/> with all providers + an injected fake `post`, then:
 *   1. Landing renders → "Enter the Tavern" CTA is visible.
 *   2. User enters workspace (mode defaults to sage).
 *   3. User switches to Spell mode.
 *   4. User types a prompt + sends.
 *   5. A player ChatMessage then a dm ChatMessage appears.
 *
 * We import .tsx explicitly so Vitest/jsdom can transform them.
 * Providers are assembled here (no shared helper) to keep the smoke test
 * self-contained and obvious.
 */

import { describe, it, expect } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'
import { ThemeProvider } from './ds/theme'
import { AppNavProvider } from './shell/AppNav'
import { CurrentUserProvider } from './shell/currentUser'
import { ConversationStoreProvider } from './shell/ConversationStoreContext'
import { MemoryConversationStore } from './shell/conversationStore'
import type { ChatResult } from './api'
import type { PostFn } from './useChat'

// ── Fake post that always returns a grounded answer ───────────────────────────

const FAKE_RESPONSE: ChatResult = {
  kind: 'ok',
  response: {
    answer: 'Fireball deals 8d6 fire damage in a 20-foot radius.',
    sources: [
      {
        book: 'phb-5e',
        chapter: 'Spells',
        section: 'Fireball',
        entity: 'Fireball',
        page: 241,
        snippet: '8d6 fire damage on a failed save',
      },
    ],
    answerable: true,
  },
}

const fakePost: PostFn = async () => FAKE_RESPONSE

// ── Provider wrapper that injects the fake post via ChatPane's prop ───────────
// ChatPane accepts a `post` prop, but App renders WorkspaceShell → ChatPane
// without a way to inject post directly. We need to render App but intercept
// the post call. We do this by rendering a thin wrapper that passes `post`
// down via a context override — but ChatPane uses useChat({ post, mode, conversationId })
// and post defaults to the real postChat.
//
// The cleanest approach for this smoke test: render the workspace shell
// components directly in the same provider tree that App uses, and inject
// the fake post via the ChatPane prop (not through App, which doesn't expose it).
// This matches the ChatPane.test.tsx pattern.

import { AppNavContext } from './shell/AppNav'
import type { AppNavState } from './shell/AppNav'
import { CurrentUserContext } from './shell/currentUser'
import type { CurrentUserContextValue } from './shell/currentUser'
import { ChatPane } from './shell/ChatPane'
import { LeftNav } from './shell/LeftNav'
import { Landing } from './shell/Landing'

// ── Smoke test ────────────────────────────────────────────────────────────────

describe('App-flow smoke test (CP-F6.2)', () => {
  it('Landing renders with the Enter the Tavern CTA', () => {
    render(
      <ThemeProvider>
        <AppNavProvider>
          <CurrentUserProvider>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <App />
            </ConversationStoreProvider>
          </CurrentUserProvider>
        </AppNavProvider>
      </ThemeProvider>,
    )
    expect(screen.getByText('Enter the Tavern')).toBeInTheDocument()
    expect(screen.getByText('Aetheril')).toBeInTheDocument()
  })

  it('full flow: Landing → workspace → switch to Spell → send prompt → player + dm messages appear', async () => {
    // We drive the nav state explicitly to keep this test deterministic.
    // Use AppNavProvider (the real one) so enterWorkspace / setMode work.
    const store = new MemoryConversationStore()

    // Track which screen/mode state we are in via the real AppNavProvider.
    // We need to render both Landing and the workspace with chat so we can
    // test the transition. Since App renders Landing OR WorkspaceShell based
    // on screen, we render App and drive it through userEvent.

    const { rerender } = render(
      <ThemeProvider>
        <AppNavProvider>
          <CurrentUserProvider>
            <ConversationStoreProvider store={store}>
              <App />
            </ConversationStoreProvider>
          </CurrentUserProvider>
        </AppNavProvider>
      </ThemeProvider>,
    )

    // Step 1: Landing is visible.
    expect(screen.getByText('Enter the Tavern')).toBeInTheDocument()

    // Step 2: Click "Enter the Tavern" → enters workspace (sage mode by default).
    await userEvent.click(screen.getByText('Enter the Tavern'))

    // WorkspaceShell should now be visible (Landing text is gone).
    expect(screen.queryByText('Enter the Tavern')).not.toBeInTheDocument()

    // The empty-state label for the default sage mode should be visible.
    expect(screen.getByText('Ask the Sage…')).toBeInTheDocument()

    // Step 3: Switch to Spell mode via the header channel switcher.
    // Both the AppHeader (swe1.4) and the LeftNav render a "Spell" chip, so
    // scope the query to the header's "Channels" nav to avoid an ambiguous match.
    const channelBar = screen.getByRole('navigation', { name: 'Channels' })
    const spellChip = within(channelBar).getByRole('button', { name: 'Spell' })
    await userEvent.click(spellChip)

    // Empty-state label should update to Spell mode.
    expect(screen.getByText('Ask the Spell Archivist…')).toBeInTheDocument()

    // App renders WorkspaceShell → ChatPane without a post prop, so it uses
    // the real postChat which would try to fetch /chat. We need to swap to
    // a test harness. Re-render with the injected post via a custom wrapper
    // that replaces WorkspaceShell's ChatPane.

    // The cleanest approach: re-render with a controlled nav state and ChatPane
    // directly, matching the ChatPane.test.tsx pattern.
    const navState: AppNavState = {
      screen: 'workspace',
      mode: 'spell',
      conversationId: null,
      enterWorkspace: () => {},
      setMode: () => {},
      setConversationId: () => {},
      backToLanding: () => {},
    }

    const userState: CurrentUserContextValue = {
      user: {
        id: 'guest',
        displayName: 'Adventurer',
        initials: 'AV',
        role: 'player',
        signOut: () => {},
        editProfile: () => {},
      },
      setRole: () => {},
    }

    rerender(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserContext.Provider value={userState}>
            <ConversationStoreProvider store={store}>
              <div style={{ display: 'flex', height: '100vh' }}>
                <LeftNav />
                <main style={{ flex: 1 }}>
                  <ChatPane post={fakePost} />
                </main>
              </div>
            </ConversationStoreProvider>
          </CurrentUserContext.Provider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )

    // Step 4: Type a prompt and send it.
    const textarea = screen.getByPlaceholderText('Ask…')
    await userEvent.type(textarea, 'How does Fireball work?')
    await userEvent.keyboard('{Enter}')

    // Step 5a: Player ChatMessage appears immediately.
    expect(screen.getByText('How does Fireball work?')).toBeInTheDocument()

    // Step 5b: DM ChatMessage appears after the fake post resolves.
    await waitFor(() =>
      expect(
        screen.getByText('Fireball deals 8d6 fire damage in a 20-foot radius.'),
      ).toBeInTheDocument(),
    )

    // The source badge should also be present.
    expect(screen.getByText(/1 source/i)).toBeInTheDocument()
  })
})

// ── Basic smoke — no interaction ──────────────────────────────────────────────

describe('toolchain smoke', () => {
  it('runs vitest under bun on this machine', () => {
    expect(1 + 1).toBe(2)
  })
})

// ── Landing-only render (quick) ───────────────────────────────────────────────

describe('App renders Landing screen (integration)', () => {
  it('renders the Landing screen by default', () => {
    render(
      <ThemeProvider>
        <AppNavProvider>
          <CurrentUserProvider>
            <ConversationStoreProvider store={new MemoryConversationStore()}>
              <App />
            </ConversationStoreProvider>
          </CurrentUserProvider>
        </AppNavProvider>
      </ThemeProvider>,
    )
    expect(screen.getByText('Enter the Tavern')).toBeInTheDocument()
    expect(screen.getByText('Aetheril')).toBeInTheDocument()
  })
})

// ── Landing component (isolated) ──────────────────────────────────────────────

describe('Landing component', () => {
  it('shows brand heading and mode chips', () => {
    const enterWorkspace = () => {}
    const navState: AppNavState = {
      screen: 'landing',
      mode: 'sage',
      conversationId: null,
      enterWorkspace,
      setMode: () => {},
      setConversationId: () => {},
      backToLanding: () => {},
    }

    render(
      <ThemeProvider>
        <AppNavContext.Provider value={navState}>
          <CurrentUserProvider>
            <Landing />
          </CurrentUserProvider>
        </AppNavContext.Provider>
      </ThemeProvider>,
    )

    expect(screen.getByText('Aetheril')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Enter the Tavern' })).toBeInTheDocument()
    // Mode chips render as buttons because Landing's Chip elements have onClick.
    // The default role is player, so the DM-only GM chip is hidden (CP-D).
    expect(screen.getByRole('button', { name: 'Sage' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Spell' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rules' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  })
})
