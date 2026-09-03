import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppNavContext } from './AppNav'
import type { AppNavState } from './AppNav'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { ThemeProvider } from '../ds/theme'
import { ModelPicker, type GetModelsFn } from './ModelPicker'

// ── b8o.2 — per-conversation model picker ────────────────────────────────────

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'gpt-4o-mini', display_name: 'GPT-4o mini', tier: 'economy', supports_attachments: true },
  ],
}

function makeNavState(overrides: Partial<AppNavState> = {}): AppNavState {
  return {
    screen: 'workspace',
    mode: 'sage',
    conversationId: null,
    enterWorkspace: vi.fn(),
    setMode: vi.fn(),
    setConversationId: vi.fn(),
    backToLanding: vi.fn(),
    openProfile: vi.fn(),
    backToWorkspace: vi.fn(),
    ...overrides,
  }
}

function renderPicker(
  navState: AppNavState,
  store: MemoryConversationStore,
  opts: { getModels?: GetModelsFn; confirmChange?: (message: string) => boolean } = {},
) {
  const getModels = opts.getModels ?? (async () => CATALOG)
  return render(
    <ThemeProvider>
      <AppNavContext.Provider value={navState}>
        <ConversationStoreProvider store={store}>
          <ModelPicker getModels={getModels} confirmChange={opts.confirmChange} />
        </ConversationStoreProvider>
      </AppNavContext.Provider>
    </ThemeProvider>,
  )
}

describe('ModelPicker', () => {
  it('is disabled when there is no active conversation', () => {
    const store = new MemoryConversationStore()
    renderPicker(makeNavState({ conversationId: null }), store)
    expect(screen.getByRole('combobox', { name: /model/i })).toBeDisabled()
  })

  it('lists the enabled catalog entries fetched from getModels', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    renderPicker(makeNavState({ conversationId: conv.id }), store)
    await waitFor(() => expect(screen.getByRole('option', { name: 'GPT-4o mini' })).toBeInTheDocument())
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeInTheDocument()
  })

  it("shows the active conversation's current preference selected", async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'gpt-4o-mini')
    renderPicker(makeNavState({ conversationId: conv.id }), store)
    await waitFor(() => {
      const select = screen.getByRole('combobox', { name: /model/i }) as HTMLSelectElement
      expect(select.value).toBe('gpt-4o-mini')
    })
  })

  it('changing the preference before the first prompt updates the conversation directly, no confirmation', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    const confirmChange = vi.fn(() => true)
    renderPicker(makeNavState({ conversationId: conv.id }), store, { confirmChange })
    await waitFor(() => screen.getByRole('option', { name: 'GPT-4o mini' }))

    const user = userEvent.setup()
    await user.selectOptions(screen.getByRole('combobox', { name: /model/i }), 'gpt-4o-mini')

    expect(confirmChange).not.toHaveBeenCalled()
    expect(store.get(conv.id)?.modelPreference).toBe('gpt-4o-mini')
  })

  it('changing the preference after the first prompt asks for confirmation before starting a new conversation', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    store.recordFirstPrompt(conv.id, 'What is a basilisk?')
    const setConversationId = vi.fn()
    const confirmChange = vi.fn(() => true)
    renderPicker(
      makeNavState({ conversationId: conv.id, setConversationId }), store, { confirmChange },
    )
    await waitFor(() => screen.getByRole('option', { name: 'GPT-4o mini' }))

    const user = userEvent.setup()
    await user.selectOptions(screen.getByRole('combobox', { name: /model/i }), 'gpt-4o-mini')

    expect(confirmChange).toHaveBeenCalledTimes(1)
    // The ORIGINAL conversation is untouched — a started conversation's
    // strategy is bound server-side and must not silently change.
    expect(store.get(conv.id)?.modelPreference).toBe('auto')
    expect(setConversationId).toHaveBeenCalledTimes(1)
    const newId = setConversationId.mock.calls[0][0] as string
    expect(newId).not.toBe(conv.id)
    expect(store.get(newId)?.modelPreference).toBe('gpt-4o-mini')
  })

  it('declining the confirmation leaves everything unchanged', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    store.recordFirstPrompt(conv.id, 'What is a basilisk?')
    const setConversationId = vi.fn()
    const confirmChange = vi.fn(() => false)
    renderPicker(
      makeNavState({ conversationId: conv.id, setConversationId }), store, { confirmChange },
    )
    await waitFor(() => screen.getByRole('option', { name: 'GPT-4o mini' }))

    const user = userEvent.setup()
    await user.selectOptions(screen.getByRole('combobox', { name: /model/i }), 'gpt-4o-mini')

    expect(store.get(conv.id)?.modelPreference).toBe('auto')
    expect(setConversationId).not.toHaveBeenCalled()
  })

  it('falls back to Automatic-only if the catalog fetch fails', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage')
    renderPicker(makeNavState({ conversationId: conv.id }), store, {
      getModels: async () => { throw new Error('network error') },
    })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Automatic' })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: 'GPT-4o mini' })).not.toBeInTheDocument()
  })
})
