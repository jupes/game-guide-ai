/**
 * Per-conversation drafts (1kg.3.3) — RAIL-25, and the privacy half of X-7.
 *
 * AE-65 is the case that matters: `/npc the ferryman` typed in the GM channel
 * must not be sent as a Sage question after a channel switch. Today's composer
 * holds one un-keyed draft, which is exactly that bug.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useConversationDrafts } from './drafts'

// jsdom 29's own storage does not expose .clear() in every runner
// configuration, and a stub is the only way to watch every write.
function makeStorageStub() {
  const store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value
    }),
    removeItem: (key: string) => {
      delete store[key]
    },
    clear: () => {},
    get length() {
      return Object.keys(store).length
    },
    key: (index: number) => Object.keys(store)[index] ?? null,
  }
}

function Harness({ conversationId }: { conversationId: string | null }) {
  const { draft, setDraft, clearDraft } = useConversationDrafts(conversationId)
  return (
    <div>
      <input aria-label="draft" value={draft} onChange={(e) => setDraft(e.target.value)} />
      <button type="button" onClick={clearDraft}>
        clear
      </button>
    </div>
  )
}

let local: ReturnType<typeof makeStorageStub>
let session: ReturnType<typeof makeStorageStub>

beforeEach(() => {
  local = makeStorageStub()
  session = makeStorageStub()
  vi.stubGlobal('localStorage', local)
  vi.stubGlobal('sessionStorage', session)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useConversationDrafts — RAIL-25', () => {
  it('AE-65: a draft stays with its conversation and comes back', async () => {
    const { rerender } = render(<Harness conversationId="gm-thread" />)
    const field = screen.getByLabelText('draft')
    await userEvent.type(field, '/npc the ferryman')
    expect(field).toHaveValue('/npc the ferryman')

    rerender(<Harness conversationId="sage-thread" />)
    expect(screen.getByLabelText('draft')).toHaveValue('')

    rerender(<Harness conversationId="gm-thread" />)
    expect(screen.getByLabelText('draft')).toHaveValue('/npc the ferryman')
  })

  it('keeps a separate draft per conversation', async () => {
    const { rerender } = render(<Harness conversationId="a" />)
    await userEvent.type(screen.getByLabelText('draft'), 'first')
    rerender(<Harness conversationId="b" />)
    await userEvent.type(screen.getByLabelText('draft'), 'second')
    rerender(<Harness conversationId="a" />)
    expect(screen.getByLabelText('draft')).toHaveValue('first')
  })

  it('gives a conversation with no id yet its own bucket', async () => {
    const { rerender } = render(<Harness conversationId={null} />)
    await userEvent.type(screen.getByLabelText('draft'), 'unscoped')
    rerender(<Harness conversationId="a" />)
    expect(screen.getByLabelText('draft')).toHaveValue('')
    rerender(<Harness conversationId={null} />)
    expect(screen.getByLabelText('draft')).toHaveValue('unscoped')
  })

  it('clears one conversation without touching another', async () => {
    const { rerender } = render(<Harness conversationId="a" />)
    await userEvent.type(screen.getByLabelText('draft'), 'first')
    rerender(<Harness conversationId="b" />)
    await userEvent.type(screen.getByLabelText('draft'), 'second')
    await userEvent.click(screen.getByRole('button', { name: 'clear' }))
    expect(screen.getByLabelText('draft')).toHaveValue('')
    rerender(<Harness conversationId="a" />)
    expect(screen.getByLabelText('draft')).toHaveValue('first')
  })

  it('clearing a conversation that never had a draft changes nothing', async () => {
    render(<Harness conversationId="a" />)
    await userEvent.click(screen.getByRole('button', { name: 'clear' }))
    expect(screen.getByLabelText('draft')).toHaveValue('')
  })

  it('X-7: a draft never reaches web storage', async () => {
    render(<Harness conversationId="gm-thread" />)
    await userEvent.type(screen.getByLabelText('draft'), 'the hooded stranger at the bar')
    expect(local.setItem).not.toHaveBeenCalled()
    expect(session.setItem).not.toHaveBeenCalled()
    expect(local.length).toBe(0)
    expect(session.length).toBe(0)
  })

  it('a write that changes nothing keeps the same state object, so nothing downstream churns', () => {
    const seen: Readonly<Record<string, string>>[] = []
    function Probe() {
      const { draft, setDraft } = useConversationDrafts('a')
      seen.push({ a: draft })
      return (
        <button type="button" onClick={() => setDraft(draft)}>
          same
        </button>
      )
    }
    render(<Probe />)
    act(() => {
      screen.getByRole('button', { name: 'same' }).click()
    })
    expect(seen.every((snapshot) => snapshot.a === '')).toBe(true)
  })
})

describe('useConversationDrafts — keys that are not ordinary strings', () => {
  it.each(['__proto__', 'constructor', 'toString', 'hasOwnProperty'])(
    'treats a conversation called %s like any other',
    async (conversationId) => {
      // Conversation ids are client-generated. On a plain object these would
      // read and write the prototype, and another conversation would see them.
      const view = render(<Harness conversationId={conversationId} />)
      expect(screen.getByLabelText('draft')).toHaveValue('')
      await userEvent.type(screen.getByLabelText('draft'), '/npc the ferryman')
      view.rerender(<Harness conversationId="another" />)
      expect(screen.getByLabelText('draft')).toHaveValue('')
      view.rerender(<Harness conversationId={conversationId} />)
      expect(screen.getByLabelText('draft')).toHaveValue('/npc the ferryman')
    },
  )

  it('keeps the not-yet-created conversation apart from every named one', async () => {
    const view = render(<Harness conversationId={null} />)
    await userEvent.type(screen.getByLabelText('draft'), 'first thoughts')
    for (const named of ['', 'null', 'unscoped']) {
      view.rerender(<Harness conversationId={named} />)
      expect(screen.getByLabelText('draft')).toHaveValue('')
    }
    view.rerender(<Harness conversationId={null} />)
    expect(screen.getByLabelText('draft')).toHaveValue('first thoughts')
  })
})
