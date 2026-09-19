/**
 * ToolComposer (1kg.3.3) — the acceptance examples of record §15 that name the
 * rail, the slash menu, More or the pins, plus the keyboard table of §4.4.
 *
 * `onInvoke` is the only thing in these tests that could cost money, so every
 * "nothing is sent" case asserts it was never called. That is X-1 as a test.
 */

import { describe, it, expect, vi } from 'vitest'
import * as React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ToolId } from './contracts'
import { REGISTRY, toolAvailability } from './registry'
import { ToolComposer, WAITING_MESSAGE } from './ToolComposer'
import type { ToolComposerProps } from './ToolComposer'
import { BRIEF_HINT, NO_CAMPAIGN_MESSAGE } from './slash'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

type HarnessProps = Partial<Omit<ToolComposerProps, 'pins' | 'onPinsChange' | 'draft' | 'onDraftChange'>> & {
  initialDraft?: string
  initialPins?: ToolId[]
  onPins?: (pins: ToolId[]) => void
}

function Harness({ initialDraft = '', initialPins, onInvoke, onChat, onPins, ...rest }: HarnessProps) {
  const [draft, setDraft] = React.useState(initialDraft)
  const [pins, setPins] = React.useState<ToolId[]>(initialPins ?? [...REGISTRY.default_pinned])
  return (
    <ToolComposer
      pins={pins}
      onPinsChange={(next) => {
        setPins(next)
        onPins?.(next)
      }}
      availability={ALL_ENABLED}
      draft={draft}
      onDraftChange={setDraft}
      onInvoke={onInvoke ?? vi.fn()}
      onChat={onChat ?? vi.fn()}
      announceDelayMs={0}
      {...rest}
    />
  )
}

function composer() {
  return screen.getByRole('textbox', { name: 'Message' })
}

function menu() {
  return screen.queryByRole('listbox', { name: 'Tools' })
}

// ── §15: arming ──────────────────────────────────────────────────────────────

describe('ToolComposer — a tap arms, nothing runs (AE-1, RAIL-1, X-1)', () => {
  it('AE-1: tapping NPC writes `/npc `, focuses the composer, shows the armed row and sends nothing', async () => {
    const onInvoke = vi.fn()
    const onChat = vi.fn()
    render(<Harness onInvoke={onInvoke} onChat={onChat} />)
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))

    expect(composer()).toHaveValue('/npc ')
    expect(composer()).toHaveFocus()
    const armedRow = document.querySelector('.gm-composer__armed')
    expect(armedRow).toHaveAttribute('role', 'status')
    expect(armedRow).toHaveTextContent('NPC')
    expect(armedRow).toHaveTextContent('Describe it, then press Enter')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(onChat).not.toHaveBeenCalled()
  })

  it('puts the caret at the end, so the next keystroke is the brief', async () => {
    render(<Harness />)
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))
    const field = composer() as HTMLTextAreaElement
    expect(field.selectionStart).toBe('/npc '.length)
    await userEvent.keyboard('the ferryman')
    expect(field).toHaveValue('/npc the ferryman')
  })

  it('AE-2: picking Encounter from More keeps the typed text and leaves focus in the composer', async () => {
    render(<Harness initialDraft="the hooded stranger" />)
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    await userEvent.click(screen.getByRole('menuitem', { name: /balanced encounter/i }))
    expect(composer()).toHaveValue('/encounter the hooded stranger')
    expect(composer()).toHaveFocus()
  })

  it('renames Send after what it would run (RAIL-3)', async () => {
    render(<Harness />)
    expect(screen.getByRole('button', { name: 'Send message' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))
    expect(screen.getByRole('button', { name: 'Run NPC tool' })).toBeInTheDocument()
  })
})

// ── §15: submitting ──────────────────────────────────────────────────────────

describe('ToolComposer — submitting (§4.2, §15)', () => {
  it('AE-3: `/npc ` and Enter sends nothing and announces the brief hint', async () => {
    const onInvoke = vi.fn()
    render(<Harness initialDraft="/npc " onInvoke={onInvoke} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(screen.getByText(BRIEF_HINT)).toBeInTheDocument()
    expect(composer()).toHaveValue('/npc ')
  })

  it('AE-4: `/recap ` and Enter sends one invocation with an empty brief', async () => {
    const onInvoke = vi.fn()
    render(<Harness initialDraft="/recap " onInvoke={onInvoke} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).toHaveBeenCalledTimes(1)
    expect(onInvoke).toHaveBeenCalledWith({ tool: expect.objectContaining({ id: 'recap' }), brief: '' })
    expect(composer()).toHaveValue('')
  })

  it('AE-5: `/monster CR 5, drowned` invokes, and the menu closed at the first space', async () => {
    const onInvoke = vi.fn()
    render(<Harness onInvoke={onInvoke} />)
    await userEvent.type(composer(), '/monster')
    expect(menu()).toBeInTheDocument()
    await userEvent.type(composer(), ' CR 5, drowned')
    expect(menu()).not.toBeInTheDocument()
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).toHaveBeenCalledWith({ tool: expect.objectContaining({ id: 'monster' }), brief: 'CR 5, drowned' })
  })

  it('AE-6: `AC 15/16 seems high` is an ordinary chat turn and the menu never opened', async () => {
    const onChat = vi.fn()
    const onInvoke = vi.fn()
    render(<Harness onChat={onChat} onInvoke={onInvoke} />)
    await userEvent.type(composer(), 'AC 15/16 seems high')
    expect(menu()).not.toBeInTheDocument()
    await userEvent.type(composer(), '{Enter}')
    expect(onChat).toHaveBeenCalledWith('AC 15/16 seems high')
    expect(onInvoke).not.toHaveBeenCalled()
  })

  it('AE-7: `/npcs a guard` sends nothing, keeps the draft and mentions `//`', async () => {
    const onInvoke = vi.fn()
    const onChat = vi.fn()
    render(<Harness initialDraft="/npcs a guard" onInvoke={onInvoke} onChat={onChat} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(onChat).not.toHaveBeenCalled()
    expect(composer()).toHaveValue('/npcs a guard')
    expect(screen.getByText(/unknown tool "\/npcs"/i)).toHaveTextContent('//')
  })

  it('SLASH-5: `//shrug` reaches chat as `/shrug`', async () => {
    const onChat = vi.fn()
    render(<Harness initialDraft="//shrug" onChat={onChat} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onChat).toHaveBeenCalledWith('/shrug')
  })

  it('AE-8: `/rec` takes two presses — one completes the command, one runs it (SLASH-11)', async () => {
    const onInvoke = vi.fn()
    render(<Harness onInvoke={onInvoke} />)
    await userEvent.type(composer(), '/rec')
    await userEvent.type(composer(), '{Enter}')
    expect(composer()).toHaveValue('/recap ')
    expect(onInvoke).not.toHaveBeenCalled()
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).toHaveBeenCalledTimes(1)
  })

  it('clears the last refusal as soon as the draft changes', async () => {
    render(<Harness initialDraft="/npc " />)
    await userEvent.type(composer(), '{Enter}')
    expect(screen.getByText(BRIEF_HINT)).toBeInTheDocument()
    await userEvent.type(composer(), 'a')
    expect(screen.queryByText(BRIEF_HINT)).not.toBeInTheDocument()
  })

  it('Send does the same thing Enter does', async () => {
    const onInvoke = vi.fn()
    render(<Harness initialDraft="/monster a drowned thing" onInvoke={onInvoke} />)
    await userEvent.click(screen.getByRole('button', { name: 'Run Monster tool' }))
    expect(onInvoke).toHaveBeenCalledTimes(1)
  })

  it('an empty draft sends nothing at all', async () => {
    const onChat = vi.fn()
    render(<Harness onChat={onChat} />)
    await userEvent.type(composer(), '   {Enter}')
    expect(onChat).not.toHaveBeenCalled()
  })
})

// ── §4.4: the keyboard table ─────────────────────────────────────────────────

describe('ToolComposer — the slash menu keyboard (§4.4)', () => {
  async function openMenu(token = '/') {
    render(<Harness />)
    await userEvent.type(composer(), token)
    return screen.getByRole('listbox', { name: 'Tools' })
  }

  function activeOption() {
    const id = composer().getAttribute('aria-activedescendant')
    return id ? document.getElementById(id) : null
  }

  it('opens while the draft is a partial command and the composer has focus (SLASH-7)', async () => {
    await openMenu()
    expect(menu()).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(REGISTRY.tools.length)
  })

  it('closes on blur, because the menu belongs to a focused composer (SLASH-8)', async () => {
    await openMenu()
    fireEvent.blur(composer())
    expect(menu()).not.toBeInTheDocument()
  })

  it('closes on an outside click (SLASH-8)', async () => {
    await openMenu()
    await userEvent.click(document.body)
    expect(menu()).not.toBeInTheDocument()
  })

  it('ArrowDown and ArrowUp move the active option, wrapping', async () => {
    await openMenu()
    expect(activeOption()).toHaveTextContent('/npc')
    await userEvent.keyboard('{ArrowDown}')
    expect(activeOption()).toHaveTextContent('/monster')
    await userEvent.keyboard('{ArrowUp}{ArrowUp}')
    expect(activeOption()).toHaveTextContent(REGISTRY.tools[REGISTRY.tools.length - 1].command)
    await userEvent.keyboard('{ArrowDown}')
    expect(activeOption()).toHaveTextContent('/npc')
  })

  it('Home and End jump to the first and last option', async () => {
    await openMenu()
    await userEvent.keyboard('{End}')
    expect(activeOption()).toHaveTextContent(REGISTRY.tools[REGISTRY.tools.length - 1].command)
    await userEvent.keyboard('{Home}')
    expect(activeOption()).toHaveTextContent('/npc')
  })

  it('Enter accepts the active option and never submits (SLASH-11)', async () => {
    const onInvoke = vi.fn()
    const onChat = vi.fn()
    render(<Harness onInvoke={onInvoke} onChat={onChat} />)
    await userEvent.type(composer(), '/mo')
    await userEvent.keyboard('{Enter}')
    expect(composer()).toHaveValue('/monster ')
    expect(menu()).not.toBeInTheDocument()
    expect(onInvoke).not.toHaveBeenCalled()
    expect(onChat).not.toHaveBeenCalled()
  })

  it('Tab accepts, the same as Enter', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/mo')
    await userEvent.keyboard('{Tab}')
    expect(composer()).toHaveValue('/monster ')
  })

  it('Shift+Enter is a newline and closes the menu', async () => {
    const onInvoke = vi.fn()
    render(<Harness onInvoke={onInvoke} />)
    await userEvent.type(composer(), '/mo')
    await userEvent.keyboard('{Shift>}{Enter}{/Shift}')
    expect(composer()).toHaveValue('/mo\n')
    expect(menu()).not.toBeInTheDocument()
    expect(onInvoke).not.toHaveBeenCalled()
  })

  it('AE-9: Escape closes and keeps the draft; typing reopens the menu', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/m')
    await userEvent.keyboard('{Escape}')
    expect(menu()).not.toBeInTheDocument()
    expect(composer()).toHaveValue('/m')
    await userEvent.keyboard('o')
    expect(menu()).toBeInTheDocument()
    expect(composer()).toHaveValue('/mo')
  })

  it('Escape never clears the draft (§4.4)', async () => {
    render(<Harness initialDraft="/npc the ferryman" />)
    composer().focus()
    await userEvent.keyboard('{Escape}')
    expect(composer()).toHaveValue('/npc the ferryman')
  })

  it('hovering an option makes it active', async () => {
    await openMenu()
    await userEvent.hover(screen.getAllByRole('option')[4])
    expect(activeOption()).toHaveTextContent(REGISTRY.tools[4].command)
  })

  it('clicking an option accepts it and keeps focus in the composer', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/mo')
    await userEvent.click(screen.getByRole('option', { name: /monster/i }))
    expect(composer()).toHaveValue('/monster ')
    expect(composer()).toHaveFocus()
  })

  it('leaves the keys alone while an IME is composing (§4.4)', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/m')
    fireEvent.keyDown(composer(), { key: 'ArrowDown', isComposing: true })
    expect(composer().getAttribute('aria-activedescendant')).toBe(
      document.querySelector('[role="option"]')?.id,
    )
  })

  it('shows the no-match row rather than vanishing, and Enter then refuses (SLASH-9)', async () => {
    const onInvoke = vi.fn()
    render(<Harness onInvoke={onInvoke} />)
    await userEvent.type(composer(), '/xyz')
    expect(screen.getByRole('option')).toHaveTextContent('No tool matches "/xyz"')
    expect(composer()).not.toHaveAttribute('aria-activedescendant')
    await userEvent.keyboard('{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(screen.getByText(/unknown tool "\/xyz"/i)).toBeInTheDocument()
  })

  it('leaves the arrows, Home and End to the caret when there is nothing to move over', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/xyz')
    for (const key of ['{ArrowDown}', '{ArrowUp}', '{Home}', '{End}']) {
      await userEvent.keyboard(key)
    }
    expect(composer()).toHaveValue('/xyz')
    expect(composer()).not.toHaveAttribute('aria-activedescendant')
    expect(screen.getByRole('option')).toHaveTextContent('No tool matches "/xyz"')
  })

  it('leaves Tab to move focus when the menu has nothing acceptable', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/xyz')
    await userEvent.tab()
    expect(composer()).toHaveValue('/xyz')
    expect(composer()).not.toHaveFocus()
  })

  it('will not accept a disabled option, and Enter falls through to submit (SLASH-10)', async () => {
    const onInvoke = vi.fn()
    render(<Harness availability={DEFAULTS} onInvoke={onInvoke} />)
    await userEvent.type(composer(), '/portrait')
    expect(screen.getByRole('option', { name: /portrait/i })).toHaveAttribute('aria-disabled', 'true')
    await userEvent.keyboard('{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(
      screen.getByText("Image generation isn't set up yet.", { selector: '.gm-composer__message' }),
    ).toBeInTheDocument()
  })
})

// ── SLASH-12: the ARIA wiring ────────────────────────────────────────────────

describe('ToolComposer — screen-reader wiring (SLASH-12)', () => {
  it('keeps the textarea a textbox: no combobox, no aria-expanded', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(composer()).not.toHaveAttribute('aria-expanded')
    expect(composer()).toHaveAttribute('aria-autocomplete', 'list')
  })

  it('points aria-controls at the listbox and aria-activedescendant at the active option', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/')
    const listbox = screen.getByRole('listbox', { name: 'Tools' })
    expect(composer()).toHaveAttribute('aria-controls', listbox.id)
    const active = composer().getAttribute('aria-activedescendant')
    expect(document.getElementById(active ?? '')).toHaveAttribute('aria-selected', 'true')
  })

  it('drops aria-controls once the menu closes', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/')
    await userEvent.keyboard('{Escape}')
    expect(composer()).not.toHaveAttribute('aria-controls')
    expect(composer()).not.toHaveAttribute('aria-activedescendant')
  })

  it('announces the option count and the active option politely', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/')
    const live = document.querySelector('.gm-composer__sr-only')
    expect(live).toHaveAttribute('aria-live', 'polite')
    await waitFor(() => {
      expect(live).toHaveTextContent(`${REGISTRY.tools.length} tools`)
      expect(live).toHaveTextContent('NPC. Generate an NPC dossier.')
    })
  })

  it('announces a disabled option with its reason', async () => {
    render(<Harness availability={DEFAULTS} />)
    await userEvent.type(composer(), '/portrait')
    await waitFor(() => {
      expect(document.querySelector('.gm-composer__sr-only')).toHaveTextContent(
        "Image generation isn't set up yet.",
      )
    })
  })

  it('counts one match in the singular', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/enc')
    await waitFor(() => {
      expect(document.querySelector('.gm-composer__sr-only')).toHaveTextContent('1 tool. Encounter.')
    })
  })

  it('announces a miss', async () => {
    render(<Harness />)
    await userEvent.type(composer(), '/xyz')
    await waitFor(() => {
      expect(document.querySelector('.gm-composer__sr-only')).toHaveTextContent('No tool matches "/xyz"')
    })
  })
})

// ── RAIL-13, RAIL-16, RAIL-6 ─────────────────────────────────────────────────

describe('ToolComposer — what blocks a submit, and what does not', () => {
  it('AE-58-adjacent: a disabled tool is refused with its reason, not sent (RAIL-10)', async () => {
    const onInvoke = vi.fn()
    render(<Harness availability={DEFAULTS} initialDraft="/portrait Ondrey" onInvoke={onInvoke} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(screen.getByText("Image generation isn't set up yet.")).toBeInTheDocument()
  })

  it('with no campaign a known command answers the campaign prompt and sends nothing (SLASH-3)', async () => {
    const onInvoke = vi.fn()
    render(<Harness campaignSelected={false} initialDraft="/npc the ferryman" onInvoke={onInvoke} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
    expect(screen.getByText(NO_CAMPAIGN_MESSAGE, { selector: '.gm-composer__message' })).toBeInTheDocument()
  })

  it('with no campaign, plain chat still works — uncampaigned GM chat is unchanged (RAIL-13)', async () => {
    const onChat = vi.fn()
    render(<Harness campaignSelected={false} initialDraft="who runs the docks?" onChat={onChat} />)
    await userEvent.type(composer(), '{Enter}')
    expect(onChat).toHaveBeenCalledWith('who runs the docks?')
  })

  it('RAIL-6: an over-length brief shows a counter and blocks the submit', async () => {
    const onInvoke = vi.fn()
    render(<Harness initialDraft={`/npc ${'a'.repeat(2001)}`} onInvoke={onInvoke} />)
    expect(screen.getByText(/2001 of 2000 characters/)).toBeInTheDocument()
    expect(composer()).toHaveAttribute('aria-invalid', 'true')
    await userEvent.type(composer(), '{Enter}')
    expect(onInvoke).not.toHaveBeenCalled()
  })

  it('RAIL-16: a working tool never disables the composer or Send', () => {
    render(<Harness initialDraft="/recap " workbenchBusy={false} />)
    expect(composer()).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Run Recap tool' })).toBeEnabled()
  })

  it('RAIL-16: the Workbench cap is the one thing that makes Send wait', () => {
    render(<Harness initialDraft="/recap " workbenchBusy />)
    expect(screen.getByRole('button', { name: 'Run Recap tool' })).toBeDisabled()
    expect(screen.getByText(WAITING_MESSAGE)).toBeInTheDocument()
    // Drafting is never blocked (X-5).
    expect(composer()).toBeEnabled()
  })

  it('RAIL-16: a pending plain chat turn makes Send wait, exactly as today', () => {
    render(<Harness initialDraft="a question" chatPending />)
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    expect(composer()).toBeEnabled()
  })

  it('CANVAS-22: with slash parsing off, a `/` draft is text and reaches chat', async () => {
    const onChat = vi.fn()
    const onInvoke = vi.fn()
    render(<Harness slashEnabled={false} initialDraft="/npc make her older" onChat={onChat} onInvoke={onInvoke} />)
    await userEvent.type(composer(), '{Enter}')
    expect(menu()).not.toBeInTheDocument()
    expect(onInvoke).not.toHaveBeenCalled()
    expect(onChat).toHaveBeenCalledWith('/npc make her older')
  })
})

// ── The three surfaces, one registry ─────────────────────────────────────────

describe('ToolComposer — rail, More and the slash menu read one registry', () => {
  it('every tool is reachable from the rail or More, and all of them from the menu', async () => {
    render(<Harness initialPins={['npc', 'monster']} />)
    const railLabels = Array.from(document.querySelectorAll('.gm-tool-rail__tool')).map((b) => b.textContent ?? '')
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    const moreLabels = screen.getAllByRole('menuitem').map((item) => item.textContent ?? '')
    await userEvent.keyboard('{Escape}')
    await userEvent.type(composer(), '/')
    const menuCommands = screen.getAllByRole('option').map((option) => option.textContent ?? '')

    for (const tool of REGISTRY.tools) {
      const onRail = railLabels.some((label) => label.includes(tool.label))
      const inMore = moreLabels.some((label) => label.includes(tool.blurb))
      expect(onRail || inMore).toBe(true)
      expect(menuCommands.some((command) => command.includes(tool.command))).toBe(true)
    }
  })

  it('arming from any surface produces the same draft', async () => {
    render(<Harness initialPins={['npc']} />)
    await userEvent.click(screen.getByRole('button', { name: /npc/i }))
    expect(composer()).toHaveValue('/npc ')

    await userEvent.clear(composer())
    await userEvent.type(composer(), '/np')
    await userEvent.keyboard('{Enter}')
    expect(composer()).toHaveValue('/npc ')

    await userEvent.clear(composer())
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    await userEvent.click(screen.getByRole('menuitem', { name: /stat block/i }))
    expect(composer()).toHaveValue('/monster ')
  })
})

// ── SLASH-15 through the composer ────────────────────────────────────────────

describe('ToolComposer — Customise rail (SLASH-15)', () => {
  it('opens from More, saves a new order, and the rail follows it', async () => {
    const onPins = vi.fn()
    render(<Harness initialPins={['npc', 'monster']} onPins={onPins} />)
    await userEvent.click(screen.getByRole('button', { name: /more/i }))
    await userEvent.click(screen.getByRole('menuitem', { name: /customise/i }))

    const dialog = screen.getByRole('dialog', { name: 'Customise rail' })
    expect(dialog).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Move Monster up' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(onPins).toHaveBeenCalledWith(['monster', 'npc'])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(Array.from(document.querySelectorAll('.gm-tool-rail__tool')).map((b) => b.textContent)).toEqual([
      expect.stringContaining('Monster'),
      expect.stringContaining('NPC'),
    ])
  })

  it('cancelling changes nothing and returns focus to More', async () => {
    const onPins = vi.fn()
    render(<Harness initialPins={['npc', 'monster']} onPins={onPins} />)
    const more = screen.getByRole('button', { name: /more/i })
    await userEvent.click(more)
    await userEvent.click(screen.getByRole('menuitem', { name: /customise/i }))
    await userEvent.keyboard('{Escape}')
    expect(onPins).not.toHaveBeenCalled()
    expect(more).toHaveFocus()
  })
})
