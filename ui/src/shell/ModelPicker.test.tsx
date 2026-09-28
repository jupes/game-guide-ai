import { describe, it, expect, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
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

// ── a6o — a stored preference the served catalog no longer lists ─────────────
// A browser that picked a model before D-9 (au3) holds its alias; /models now
// lists public ids only. ChatPane has only ever posted 'auto', so the server
// bound every such conversation to 'auto': the default is the faithful value.

const CATALOG_AFTER_D9 = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'traveller', display_name: 'Traveller', tier: 'traveller', supports_attachments: true },
  ],
}

describe('ModelPicker with a stored preference the catalog does not list (a6o)', () => {
  it('resets it to the default once the catalog loads, before the first prompt, without a new conversation', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'gpt-4o-mini')
    const setConversationId = vi.fn()
    const confirmChange = vi.fn(() => true)
    renderPicker(makeNavState({ conversationId: conv.id, setConversationId }), store, {
      getModels: async () => CATALOG_AFTER_D9, confirmChange,
    })

    await waitFor(() => expect(store.get(conv.id)?.modelPreference).toBe('auto'))
    expect((screen.getByRole('combobox', { name: /model/i }) as HTMLSelectElement).value).toBe('auto')
    expect(confirmChange).not.toHaveBeenCalled()
    expect(setConversationId).not.toHaveBeenCalled()
  })

  // agent-forge-harness-bta: once ChatPane actually sends the conversation's
  // STORED preference (rather than always 'auto' regardless of what this
  // reset did), resetting an already-bound conversation's preference would
  // make its NEXT turn request something other than what the server already
  // bound it to on its first turn — service/app.py's
  // claim_conversation_strategy 409s that mismatch (D6's Conversation
  // affinity). A conversation with a first prompt is already bound, so its
  // stored preference — even a pre-D9 alias the current catalog no longer
  // lists (a6o) — must survive untouched; #122 lets the server keep honouring
  // it for exactly this conversation.
  it('leaves an already-bound conversation preference alone, even if the catalog no longer lists it', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'gpt-4o-mini')
    store.recordFirstPrompt(conv.id, 'What is a basilisk?')
    const setConversationId = vi.fn()
    const confirmChange = vi.fn(() => true)
    renderPicker(makeNavState({ conversationId: conv.id, setConversationId }), store, {
      getModels: async () => CATALOG_AFTER_D9, confirmChange,
    })

    await waitFor(() => expect(screen.getByRole('option', { name: 'Traveller' })).toBeInTheDocument())
    // Let any reset effect the catalog load could have triggered settle.
    await act(async () => {})
    expect(store.get(conv.id)?.modelPreference).toBe('gpt-4o-mini')
    expect(confirmChange).not.toHaveBeenCalled()
    expect(setConversationId).not.toHaveBeenCalled()
  })

  it('shows the default for it, and keeps it stored, while only the offline fallback is known', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'traveller')
    const getModels = vi.fn(async () => { throw new Error('network error') })
    renderPicker(makeNavState({ conversationId: conv.id }), store, { getModels })

    await waitFor(() => expect(getModels).toHaveBeenCalled())
    await act(async () => {})
    expect((screen.getByRole('combobox', { name: /model/i }) as HTMLSelectElement).value).toBe('auto')
    expect(store.get(conv.id)?.modelPreference).toBe('traveller')
  })

  it('lets the reset conversation pick a listed model', async () => {
    const store = new MemoryConversationStore()
    const conv = store.create('sage', undefined, 'gpt-4o-mini')
    renderPicker(makeNavState({ conversationId: conv.id }), store, { getModels: async () => CATALOG_AFTER_D9 })
    await waitFor(() => expect(store.get(conv.id)?.modelPreference).toBe('auto'))

    await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: /model/i }), 'traveller')
    expect(store.get(conv.id)?.modelPreference).toBe('traveller')
  })
})
