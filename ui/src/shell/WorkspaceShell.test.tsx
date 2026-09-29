/**
 * WorkspaceShell (agent-forge-harness-0rn) — the narrow layout's modal drawer,
 * T-WS-1..16.
 *
 * jsdom owns semantics here: roles, names, attributes, focus and what is
 * mounted. It has no layout, no CSS and no `inert` behaviour, so nothing in
 * this file measures a box or proves the background refuses focus; the
 * Storybook phone stories do that in Chromium. `inert` is asserted as an
 * attribute only.
 *
 * The layout comes from a controllable `matchMedia` stub, installed per test.
 * Without it jsdom has no `matchMedia`, and the shell is 'wide' — today's
 * layout — which is what every other test in the repository relies on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as React from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { AppNavContext } from './AppNav'
import type { AppNavState, ChatMode } from './AppNav'
import { CurrentUserContext } from './currentUser'
import type { CurrentUserContextValue } from './currentUser'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { WorkspaceShell } from './WorkspaceShell'
import { installMatchMedia } from '../testing/matchMediaStub'
import type { MatchMediaStub } from '../testing/matchMediaStub'

const FIRST = 'Shield spell: what does it stop?'
const SECOND = 'Grappling, briefly'

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'sonnet', display_name: 'Sonnet — balanced' },
  ],
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** The latest navigation state the tower rendered, for assertions about the
 * store's conversationId and mode that the screen shows only indirectly. */
const probe: { nav: AppNavState | null } = { nav: null }

const user: CurrentUserContextValue = {
  user: {
    id: 'dm@aetheril.test',
    displayName: 'Alanna Quill',
    initials: 'AQ',
    role: 'dm',
    signOut: async () => true,
    editProfile: () => {},
  },
  authStatus: 'authenticated',
  retryAuthCheck: () => {},
  signIn: () => {},
  setDisplayName: () => {},
  setAvatarTone: () => {},
}

function Tower({
  store,
  initialConversationId,
}: {
  store: MemoryConversationStore
  initialConversationId: string | null
}): React.JSX.Element {
  const [mode, setMode] = React.useState<ChatMode>('sage')
  const [conversationId, setConversationId] = React.useState<string | null>(initialConversationId)
  const nav = React.useMemo<AppNavState>(
    () => ({
      screen: 'workspace',
      mode,
      conversationId,
      enterWorkspace: () => {},
      setMode,
      setConversationId,
      backToLanding: () => {},
      openProfile: () => {},
      backToWorkspace: () => {},
    }),
    [mode, conversationId],
  )
  React.useEffect(() => {
    probe.nav = nav
  }, [nav])
  return (
    <ThemeProvider initialTheme="light">
      <AppNavContext.Provider value={nav}>
        <CurrentUserContext.Provider value={user}>
          <ConversationStoreProvider store={store}>
            <WorkspaceShell />
          </ConversationStoreProvider>
        </CurrentUserContext.Provider>
      </AppNavContext.Provider>
    </ThemeProvider>
  )
}

let stub: MatchMediaStub | null = null
let fetchSpy: ReturnType<typeof vi.fn<(input: RequestInfo | URL) => Promise<Response>>>

function setLayout(narrow: boolean): void {
  act(() => stub?.set(narrow))
}

const modelsRequests = (): number =>
  fetchSpy.mock.calls.filter(([input]) => String(input).includes('/models')).length

async function renderShell(narrow: boolean, selected: number | null = 0) {
  stub = installMatchMedia(narrow)
  const store = new MemoryConversationStore()
  const ids = [store.create('sage', FIRST).id, store.create('sage', SECOND).id]
  const utils = render(
    <Tower store={store} initialConversationId={selected === null ? null : ids[selected]} />,
  )
  // The catalog and the history have answered, so nothing lands mid-test.
  await waitFor(() => expect(modelsRequests()).toBeGreaterThan(0))
  await screen.findByRole('option', { name: 'Sonnet — balanced', hidden: true })
  return { ...utils, store, ids }
}

const menuButton = (): HTMLElement =>
  screen.getByRole('button', { name: 'Open navigation' })
const dialog = (): HTMLElement => screen.getByRole('dialog', { name: 'Navigation' })
const queryDialog = (): HTMLElement | null => screen.queryByRole('dialog', { name: 'Navigation' })
const main = (): HTMLElement => screen.getByRole('main')
const inertNodes = (container: HTMLElement): Element[] => Array.from(container.querySelectorAll('[inert]'))

async function openDrawer(): Promise<HTMLElement> {
  await userEvent.click(menuButton())
  return dialog()
}

/** Node 25's own file-less `localStorage` can shadow jsdom's and has no
 * methods; clear only a real Storage. */
function clearStorage(storage: Storage): void {
  if (typeof storage.clear === 'function') storage.clear()
}

beforeEach(() => {
  probe.nav = null
  // Storage outlives a test in jsdom; T-WS-13 must start from a clean slate, or
  // a write an earlier test already made (same key, same value) hides a new one.
  clearStorage(localStorage)
  clearStorage(sessionStorage)
  fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/models')) return json(CATALOG)
    if (url.includes('/messages')) return json({ conversation_id: 'shell', messages: [] })
    if (url.includes('/attachments')) return json({ conversation_id: 'shell', attachments: [] })
    return json({ detail: `unrouted: ${url}` }, 404)
  })
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  stub?.restore()
  stub = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('data-theme')
})

describe('the menu button and the drawer host (T-WS-1, T-WS-16)', () => {
  it('narrow: a closed disclosure that controls the host, which has no role yet', async () => {
    await renderShell(true)
    const button = menuButton()
    expect(button).toHaveAttribute('aria-expanded', 'false')
    const host = document.getElementById(button.getAttribute('aria-controls') ?? '')
    expect(host).not.toBeNull()
    expect(host).toHaveClass('workspace-shell__nav')
    expect(host).not.toHaveAttribute('role')
    expect(host?.closest('.workspace-shell')).toHaveAttribute('data-layout', 'narrow')
  })

  it('wide: no menu button, no dialog, no inert, no scrim — the workspace as it was', async () => {
    const { container } = await renderShell(false)
    expect(screen.queryByRole('button', { name: 'Open navigation' })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(inertNodes(container)).toHaveLength(0)
    expect(container.querySelector('.workspace-shell__scrim')).toBeNull()
    expect(container.querySelector('.workspace-shell')).toHaveAttribute('data-layout', 'wide')
    expect(screen.getByRole('button', { name: 'New conversation' })).toBeInTheDocument()
  })
})

describe('opening and closing (T-WS-2, 3, 7, 8)', () => {
  it('opens as a named modal dialog, takes focus, and makes the background inert', async () => {
    const { container } = await renderShell(true)
    const button = menuButton()
    await userEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    const drawer = dialog()
    expect(drawer).toBe(document.getElementById(button.getAttribute('aria-controls') ?? ''))
    expect(drawer).toHaveAttribute('aria-modal', 'true')
    expect(document.activeElement).toBe(drawer)
    expect(container.querySelector('.workspace-shell__chrome')).toHaveAttribute('inert')
    expect(main()).toHaveAttribute('inert')
  })

  it('Escape on a control inside closes it, clears inert and returns focus to the menu button', async () => {
    const { container } = await renderShell(true)
    const drawer = await openDrawer()
    within(drawer).getByRole('button', { name: 'New conversation' }).focus()
    await userEvent.keyboard('{Escape}')
    expect(queryDialog()).toBeNull()
    expect(menuButton()).toHaveAttribute('aria-expanded', 'false')
    expect(inertNodes(container)).toHaveLength(0)
    expect(document.activeElement).toBe(menuButton())
  })

  it('Close navigation closes it and returns focus to the menu button', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: 'Close navigation' }))
    expect(queryDialog()).toBeNull()
    expect(document.activeElement).toBe(menuButton())
  })

  it('a press on the scrim closes it', async () => {
    const { container } = await renderShell(true)
    await openDrawer()
    const scrim = container.querySelector('.workspace-shell__scrim')
    expect(scrim).not.toBeNull()
    if (scrim) await userEvent.click(scrim)
    expect(queryDialog()).toBeNull()
  })
})

describe('Escape precedence (T-WS-4, 4b, 4c, 5)', () => {
  it('Escape in the rename input cancels the rename and leaves the drawer open', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: `Rename ${FIRST}` }))
    const input = within(drawer).getByRole('textbox', { name: `Conversation title for ${FIRST}` })
    expect(document.activeElement).toBe(input)
    await userEvent.keyboard('{Escape}')
    expect(within(drawer).queryByRole('textbox', { name: `Conversation title for ${FIRST}` })).toBeNull()
    expect(dialog()).toBe(drawer)
  })

  it('once focus has fallen to <body>, the next Escape still closes the drawer', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: `Rename ${FIRST}` }))
    await userEvent.keyboard('{Escape}')
    expect(dialog()).toBe(drawer)
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(queryDialog()).toBeNull()
    expect(document.activeElement).toBe(menuButton())
  })

  it('listens on the document only while open: the listener it adds is removed on close', async () => {
    await renderShell(true)
    const added = vi.spyOn(document, 'addEventListener')
    const removed = vi.spyOn(document, 'removeEventListener')
    await openDrawer()
    const keydownAdds = added.mock.calls.filter(([type]) => type === 'keydown')
    expect(keydownAdds).toHaveLength(1)
    await userEvent.click(within(dialog()).getByRole('button', { name: 'Close navigation' }))
    const listener = keydownAdds[0][1]
    expect(removed.mock.calls.some(([type, fn]) => type === 'keydown' && fn === listener)).toBe(true)
    expect(() => fireEvent.keyDown(document, { key: 'Escape' })).not.toThrow()
    expect(queryDialog()).toBeNull()
    expect(menuButton()).toHaveAttribute('aria-expanded', 'false')
    expect(document.activeElement).toBe(menuButton())
  })

  it('Escape closes the user menu first; a second Escape closes the drawer', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    const trigger = within(drawer).getByRole('button', { name: 'Open user menu' })
    await userEvent.click(trigger)
    expect(within(drawer).getByRole('group', { name: 'User menu' })).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(within(drawer).queryByRole('group', { name: 'User menu' })).toBeNull()
    expect(dialog()).toBe(drawer)
    expect(document.activeElement).toBe(trigger)
    await userEvent.keyboard('{Escape}')
    expect(queryDialog()).toBeNull()
  })
})

describe('what closes the drawer: navigation only (T-WS-6, INF-8)', () => {
  it('picking a conversation closes it and opens that conversation', async () => {
    const { ids } = await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: SECOND }))
    expect(queryDialog()).toBeNull()
    expect(probe.nav?.conversationId).toBe(ids[1])
  })

  it('picking a mode closes it and switches the mode', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: 'Spell' }))
    expect(queryDialog()).toBeNull()
    expect(probe.nav?.mode).toBe('spell')
  })

  it('New conversation closes it and selects the new conversation', async () => {
    const { ids } = await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: 'New conversation' }))
    expect(queryDialog()).toBeNull()
    expect(probe.nav?.conversationId).not.toBeNull()
    expect(ids).not.toContain(probe.nav?.conversationId)
  })

  it('starting a rename or switching the theme leaves it open', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: `Rename ${FIRST}` }))
    expect(dialog()).toBe(drawer)
    const theme = within(drawer).getByRole('switch', { name: 'Dark theme' })
    await userEvent.click(theme)
    expect(theme).toBeChecked()
    expect(dialog()).toBe(drawer)
  })

  it('a confirmed model change starts a new conversation and leaves the drawer open', async () => {
    const { ids } = await renderShell(true)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const drawer = await openDrawer()
    const picker = within(drawer).getByRole('combobox', { name: 'Model' })
    await userEvent.selectOptions(picker, 'sonnet')
    expect(window.confirm).toHaveBeenCalled()
    await waitFor(() => expect(ids).not.toContain(probe.nav?.conversationId))
    expect(dialog()).toBe(drawer)
  })
})

describe('the modal Tab wrap, integrated (T-WS-9)', () => {
  it('Tab from the last stop (the user-menu trigger) reaches Close navigation', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    within(drawer).getByRole('button', { name: 'Open user menu' }).focus()
    await userEvent.tab()
    expect(document.activeElement).toBe(within(drawer).getByRole('button', { name: 'Close navigation' }))
  })
})

describe('where the model and theme controls live (T-WS-10, T-WS-15)', () => {
  it('narrow: not in the channel band; exactly one picker, in the (hidden) drawer host', async () => {
    await renderShell(true)
    const channels = screen.getByRole('navigation', { name: 'Channels' })
    expect(within(channels).queryByRole('combobox', { name: 'Model' })).toBeNull()
    expect(within(channels).queryByRole('switch', { name: 'Dark theme' })).toBeNull()
    const pickers = screen.getAllByRole('combobox', { name: 'Model' })
    expect(pickers).toHaveLength(1)
    expect(pickers[0].closest('.workspace-shell__nav')).not.toBeNull()
  })

  it('wide: exactly one picker, in the channel band, and no Settings group', async () => {
    await renderShell(false)
    const pickers = screen.getAllByRole('combobox', { name: 'Model' })
    expect(pickers).toHaveLength(1)
    expect(within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('combobox', { name: 'Model' })).toBe(
      pickers[0],
    )
    expect(screen.queryByRole('group', { name: 'Settings' })).toBeNull()
  })

  it('narrow: the catalog loads before the drawer is ever opened', async () => {
    await renderShell(true)
    expect(menuButton()).toHaveAttribute('aria-expanded', 'false')
    expect(modelsRequests()).toBeGreaterThanOrEqual(1)
  })
})

describe('crossing 768px (T-WS-11, 11b, 11c, 12, 12b, 12c, 12d; LAYOUT-6)', () => {
  it('widening with the drawer open closes it and leaves focus on the conversation button', async () => {
    const { container } = await renderShell(true)
    const drawer = await openDrawer()
    const conversation = within(drawer).getByRole('button', { name: SECOND })
    conversation.focus()
    setLayout(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(inertNodes(container)).toHaveLength(0)
    expect(document.activeElement).toBe(conversation)
  })

  it('widening with focus on the drawer’s theme switch moves focus to <main>', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    within(drawer).getByRole('switch', { name: 'Dark theme' }).focus()
    setLayout(false)
    expect(document.activeElement).toBe(main())
  })

  it('narrowing again after widening starts closed', async () => {
    const { container } = await renderShell(true)
    const drawer = await openDrawer()
    setLayout(false)
    setLayout(true)
    expect(queryDialog()).toBeNull()
    expect(menuButton()).toHaveAttribute('aria-expanded', 'false')
    expect(inertNodes(container)).toHaveLength(0)
    expect(document.activeElement).not.toBe(drawer)
  })

  it('narrowing with focus in the sidebar moves focus to <main>', async () => {
    await renderShell(false)
    screen.getByRole('button', { name: SECOND }).focus()
    setLayout(true)
    expect(document.activeElement).toBe(main())
  })

  it('narrowing with focus on the header’s Model picker moves focus to <main>', async () => {
    await renderShell(false)
    within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('combobox', { name: 'Model' }).focus()
    setLayout(true)
    expect(document.activeElement).toBe(main())
  })

  it('narrowing with nothing focused moves nothing', async () => {
    await renderShell(false)
    expect(document.activeElement).toBe(document.body)
    setLayout(true)
    expect(document.activeElement).toBe(document.body)
  })

  it('narrowing with focus on a channel chip leaves it there', async () => {
    await renderShell(false)
    const chip = within(screen.getByRole('navigation', { name: 'Channels' })).getByRole('button', { name: 'Rules' })
    chip.focus()
    setLayout(true)
    expect(document.activeElement).toBe(chip)
  })
})

describe('nothing persists, and drafts survive (T-WS-13, T-WS-14)', () => {
  function snapshot(storage: Storage): Record<string, string | null> {
    const entries: Record<string, string | null> = {}
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i)
      if (key !== null) entries[key] = storage.getItem(key)
    }
    return entries
  }

  it('opening and closing writes no storage, URL or history entry', async () => {
    await renderShell(true)
    const before = {
      local: snapshot(localStorage),
      session: snapshot(sessionStorage),
      href: window.location.href,
      history: window.history.length,
    }
    await openDrawer()
    await userEvent.click(within(dialog()).getByRole('button', { name: 'Close navigation' }))
    await openDrawer()
    expect({
      local: snapshot(localStorage),
      session: snapshot(sessionStorage),
      href: window.location.href,
      history: window.history.length,
    }).toEqual(before)
  })

  it('a rename draft and a composer draft survive crossing 768px both ways', async () => {
    await renderShell(true)
    const drawer = await openDrawer()
    await userEvent.click(within(drawer).getByRole('button', { name: `Rename ${FIRST}` }))
    const input = within(drawer).getByRole('textbox', { name: `Conversation title for ${FIRST}` })
    await userEvent.clear(input)
    await userEvent.type(input, 'Draf')
    setLayout(false)
    expect(screen.getByRole('textbox', { name: `Conversation title for ${FIRST}` })).toHaveValue('Draf')

    const composer = screen.getByPlaceholderText('Ask…')
    await userEvent.type(composer, 'half a question')
    setLayout(true)
    expect(screen.getByPlaceholderText('Ask…')).toHaveValue('half a question')
    expect(screen.getByPlaceholderText('Ask…')).toBe(composer)
  })
})
