/**
 * GameDocument (agent-forge-harness-1kg.6.2) — the config-driven document shell.
 *
 * The acceptance criteria of the bead, one describe block each:
 *
 * 1. all eight types render without a raw key or an HTML entity;
 * 2. editable fields are labelled, validated, stateful and reachable;
 * 3. structured values never flatten;
 * 4. the SelectionBar is scoped and focused accessibly;
 * 5. the gold wash tracks the keys it was given, and nothing else;
 * 6. nothing loads a remote subresource, and nothing reaches web storage.
 */

import { describe, expect, it, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { DOCUMENT_TYPE_IDS, type DocumentTypeId } from './contracts'
import { REGISTRY, documentTypeById } from './registry'
import { documentFieldReads } from './documentFields'
import { documentFixture, hydratedFixture } from './documentFixtures'
import { GameDocument, type GameDocumentProps } from './GameDocument'

type Options = Partial<Omit<GameDocumentProps, 'document'>> & { typeId?: DocumentTypeId; empty?: boolean }

function show({ typeId = 'npc', empty = false, ...props }: Options = {}) {
  return render(<GameDocument document={documentFixture(typeId, { empty })} {...props} />)
}

function root(): HTMLElement {
  return screen.getByRole('article')
}

/**
 * Every non-blank text node anyone can actually read.
 *
 * A Material Symbols ligature IS a snake_case word — `auto_fix_high` — but it
 * sits inside `aria-hidden="true"` and nobody reads it. Skipping hidden
 * subtrees is what makes the sweep below about escaped KEYS rather than about
 * the icon font.
 */
function textNodes(host: HTMLElement): string[] {
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT)
  const found: string[] = []
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = (node.textContent ?? '').trim()
    if (text !== '' && node.parentElement?.closest('[aria-hidden="true"]') == null) found.push(text)
  }
  return found
}

/** A field key as CANVAS-19 writes one, when it escapes into the page. */
const SNAKE_CASE = /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/
const HTML_ENTITY = /&[a-z]+;/

// ── AC 1 · all eight types ───────────────────────────────────────────────────

describe('every type renders with the registry, and never with a key', () => {
  it.each(DOCUMENT_TYPE_IDS)('%s shows the registry label for every field it declares', (id) => {
    show({ typeId: id })
    for (const field of documentFieldReads(id)) {
      expect(screen.getAllByText(field.label).length, `${id}.${field.key}`).toBeGreaterThan(0)
    }
  })

  it.each(DOCUMENT_TYPE_IDS)('%s puts no raw key on screen', (id) => {
    const keys = documentFieldReads(id).map((field) => field.key)
    // Everything that decorates a field is on, so the icon ligatures, the wash
    // and the markers are all in the tree while the sweep runs.
    show({
      typeId: id,
      changedFields: keys.slice(3, 5),
      revealedFields: keys.slice(0, 2),
      onArmFieldEdit: vi.fn(),
      onAcknowledgeChanges: vi.fn(),
    })
    const declared = new Set(keys)
    for (const text of textNodes(root())) {
      expect(SNAKE_CASE.test(text), `${id}: ${text}`).toBe(false)
      expect(declared.has(text), `${id}: ${text}`).toBe(false)
    }
  })

  it('hides every icon ligature from assistive technology', () => {
    show({ changedFields: ['wants'], revealedFields: ['voice'], onArmFieldEdit: vi.fn() })
    const ligatures = root().querySelectorAll('.material-symbols-rounded')
    expect(ligatures.length).toBeGreaterThan(2)
    for (const icon of ligatures) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it.each(DOCUMENT_TYPE_IDS)('%s puts no literal HTML entity on screen', (id) => {
    show({ typeId: id })
    for (const text of textNodes(root())) expect(HTML_ENTITY.test(text), text).toBe(false)
  })

  it.each(DOCUMENT_TYPE_IDS)('%s renders every field in the registry order', (id) => {
    show({ typeId: id })
    const order = documentFieldReads(id).map((field) => field.label)
    const rendered = Array.from(root().querySelectorAll('.gm-field__label')).map((node) => node.textContent)
    expect(rendered).toEqual(order)
  })

  it('renders a bare ampersand as itself', () => {
    show({ typeId: 'lore' })
    expect(root()).toHaveTextContent('Board & older')
  })

  it('shows a placeholder, and never an NPC, for a type it does not know (X-8)', () => {
    const stranger = { ...documentFixture('npc'), type: 'spaceship' as DocumentTypeId }
    render(<GameDocument document={stranger} />)
    expect(screen.getByText(/made with a newer version/i)).toBeInTheDocument()
    expect(screen.queryByText('NPC Dossier')).not.toBeInTheDocument()
    expect(screen.queryByText('Voice')).not.toBeInTheDocument()
  })

  it('reads the renderer, the accent, printable and citations from the registry', () => {
    show({ typeId: 'statblock' })
    expect(root()).toHaveAttribute('data-renderer', 'stat_block_card')
    show({ typeId: 'lore' })
    expect(screen.getAllByRole('article')[1]).toHaveAttribute('data-accent', 'arcane')
    show({ typeId: 'handout' })
    expect(screen.getAllByRole('article')[2]).toHaveAttribute('data-printable', 'true')
  })

  it('shows a corpus footer only for a type that cites the corpus', () => {
    const sources = [
      { book: "Player's Handbook", chapter: null, section: null, entity: 'Tides', page: 12, snippet: 'The ninth tide.' },
    ]
    show({ typeId: 'lore', sources })
    expect(screen.getByText(/1 source/)).toBeInTheDocument()
    show({ typeId: 'npc', sources })
    expect(screen.getAllByText(/1 source/)).toHaveLength(1)
  })
})

// ── AC 2 · labels, states, keyboard, screen-reader semantics ─────────────────

describe('the document has one polite live region, and it is quiet', () => {
  it('starts silent', () => {
    show()
    expect(screen.getByRole('status')).toHaveTextContent('')
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
  })

  it('does not announce a keystroke', async () => {
    const user = userEvent.setup()
    show()
    await user.click(screen.getByRole('button', { name: 'Edit Voice' }))
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), 'x')
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('names the field that saved, and the field that failed above it', () => {
    show({ fieldStates: { voice: { state: 'saved' } } })
    expect(screen.getByRole('status')).toHaveTextContent('Voice saved')
    show({ fieldStates: { voice: { state: 'saved' }, wants: { state: 'error', message: 'Too long' } } })
    expect(screen.getAllByRole('status')[1]).toHaveTextContent("Couldn't save Wants")
  })
})

describe('an AI edit holds one field and leaves the rest alone (CANVAS-24)', () => {
  it('locks only what it was given', () => {
    show({ assistantEditing: ['wants'] })
    expect(screen.getByText('Assistant is editing…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit Wants' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit Voice' })).toBeInTheDocument()
  })

  it('explains the Edit control’s disappearance to somebody who cannot see it go', () => {
    // The control is REMOVED rather than disabled — the alignment forbids
    // leaving a disabled control in an emergency — so the removal owes a
    // screen-reader user an explanation it can hear.
    show({ assistantEditing: ['wants'] })
    expect(screen.getByRole('status')).toHaveTextContent(
      'Assistant is editing Wants. You cannot edit it by hand until it finishes.',
    )
  })

  it('says nothing about a takeover when there is none', () => {
    show()
    expect(screen.getByRole('status')).toHaveTextContent('')
  })
})

describe('the reveal badge and markers are props (REVEAL-13)', () => {
  it('shows what the owner passed, and computes nothing', () => {
    show({ revealBadge: 'REVEALED', revealedFields: ['voice'] })
    expect(screen.getByText('REVEALED')).toBeInTheDocument()
    expect(screen.getByText('The table can see this')).toBeInTheDocument()
  })

  it('says GM ONLY when nothing was passed, which is the safe reading', () => {
    show()
    expect(screen.getByText('GM ONLY')).toBeInTheDocument()
  })

  it('reads an explicit null as unknown, never as GM ONLY', () => {
    // An owner writing `revealBadge={projection?.badge ?? null}` is saying
    // "I looked and could not confirm". REVEAL-13 names that case and forbids
    // rendering it as "nothing revealed": a GM told the table sees nothing
    // does not go and check.
    show({ revealBadge: null })
    expect(screen.queryByText('GM ONLY')).not.toBeInTheDocument()
    expect(screen.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
  })

  it('still repeats the owner’s own words, and computes none of them', () => {
    show({ revealBadge: 'Reveal state unknown — reconnecting' })
    expect(screen.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    expect(screen.queryByText('GM ONLY')).not.toBeInTheDocument()
  })
})

// ── AC 3 · structure survives ────────────────────────────────────────────────

describe('structured fields reach the callbacks structured', () => {
  it('commits a text_list as an array', async () => {
    const user = userEvent.setup()
    const onFieldCommit = vi.fn()
    show({ typeId: 'session-notes', onFieldCommit })
    await user.click(screen.getByRole('button', { name: 'Edit Beats' }))
    await user.click(screen.getByRole('button', { name: 'Move Beats 2 up' }))
    expect(onFieldCommit).toHaveBeenLastCalledWith('beats', [
      'Ondrey would not say who signed it',
      'The writ was struck through twice',
      'The vault door is keyed to a name',
    ])
  })

  it('commits an ability block as an object with all six keys', async () => {
    const user = userEvent.setup()
    const onFieldCommit = vi.fn()
    show({ typeId: 'character-sheet', onFieldCommit })
    await user.click(screen.getByRole('button', { name: 'Edit Ability scores' }))
    const dexterity = screen.getByRole('spinbutton', { name: 'Dexterity' })
    await user.clear(dexterity)
    await user.type(dexterity, '18')
    await user.tab()
    expect(onFieldCommit).toHaveBeenLastCalledWith('abilities', {
      str: 10,
      dex: 18,
      con: 14,
      int: 11,
      wis: 15,
      cha: 8,
    })
  })

  it('keeps a stat block`s entries apart from one another', () => {
    show({ typeId: 'statblock' })
    const actions = screen.getByRole('list', { name: 'Actions' })
    expect(within(actions).getAllByRole('listitem')).toHaveLength(2)
    expect(actions.textContent).not.toContain('Multiattack.It makes')
  })

  it('lays a stat block out as a stat block, with its ability table', () => {
    show({ typeId: 'statblock' })
    expect(screen.getByRole('table', { name: 'Ability scores' })).toBeInTheDocument()
    expect(root().querySelector('.gm-document__stats')).not.toBeNull()
  })
})

// ── AC 4 · the SelectionBar ──────────────────────────────────────────────────

function proseOf(label: string): HTMLElement {
  return screen.getByRole('group', { name: label })
}

/** A selection the GM could have made with a pointer, inside one field. */
function selectWithin(element: HTMLElement, start: number, end: number): void {
  const node = element.firstChild
  const selection = window.getSelection()
  if (node === null || selection === null) throw new Error('nothing to select')
  selection.removeAllRanges()
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, end)
  selection.addRange(range)
  act(() => document.dispatchEvent(new Event('selectionchange')))
}

function selectAcross(from: HTMLElement, to: HTMLElement): void {
  const selection = window.getSelection()
  if (from.firstChild === null || to.firstChild === null || selection === null) throw new Error('nothing to select')
  selection.removeAllRanges()
  const range = document.createRange()
  range.setStart(from.firstChild, 0)
  range.setEnd(to.firstChild, 4)
  selection.addRange(range)
  act(() => document.dispatchEvent(new Event('selectionchange')))
}

function bar(): HTMLElement | null {
  return screen.queryByRole('toolbar', { name: /selected text/ })
}

describe('the SelectionBar appears only where CANVAS-23 allows it', () => {
  it('rises for a selection inside one editable prose field', () => {
    show()
    selectWithin(proseOf('Wants'), 4, 10)
    expect(bar()).not.toBeNull()
    expect(screen.getByRole('toolbar', { name: 'Ask the assistant about the selected text in Wants' })).toBeInTheDocument()
  })

  it('raises nothing for a selection that spans two fields', () => {
    show()
    selectAcross(proseOf('Wants'), proseOf('Leverage'))
    expect(bar()).toBeNull()
  })

  it('raises nothing inside a field that cannot be edited', () => {
    const read = documentFieldReads('npc').find((field) => field.key === 'wants')
    expect(read?.editable).toBe(true)
    show({ assistantEditing: ['wants'] })
    selectWithin(proseOf('Wants'), 4, 10)
    expect(bar()).toBeNull()
  })

  it('raises nothing for an empty selection', () => {
    show()
    selectWithin(proseOf('Wants'), 6, 6)
    expect(bar()).toBeNull()
  })

  it('does not enter edit mode when text is selected', () => {
    show()
    selectWithin(proseOf('Wants'), 4, 10)
    expect(screen.queryByRole('textbox', { name: 'Wants' })).not.toBeInTheDocument()
  })

  it('reports the field, the span in code points and the text', async () => {
    const user = userEvent.setup()
    const onSelectionAction = vi.fn()
    show({ onSelectionAction })
    const wants = proseOf('Wants')
    selectWithin(wants, 4, 10)
    await user.click(screen.getByRole('button', { name: 'Shorter' }))
    expect(onSelectionAction).toHaveBeenCalledWith('shorter', {
      field: 'wants',
      label: 'Wants',
      start: 4,
      end: 10,
      text: (wants.textContent ?? '').slice(4, 10),
    })
  })

  it('counts an astral character once, so the span matches its text', async () => {
    const user = userEvent.setup()
    const onSelectionAction = vi.fn()
    show({ onSelectionAction })
    // `notes` holds `Rolls a 🎲 for every promise…`; the die is one code point
    // and two UTF-16 units, so the span must not be the DOM's own offsets.
    selectWithin(proseOf('Notes'), 8, 14)
    await user.click(screen.getByRole('button', { name: 'Rewrite' }))
    const reported = onSelectionAction.mock.calls[0][1]
    expect(reported.end - reported.start).toBe([...reported.text].length)
    expect(reported.text).toContain('\u{1F3B2}')
  })

  it('follows its field in the tab order and returns focus on Escape', async () => {
    const user = userEvent.setup()
    show()
    const wants = proseOf('Wants')
    wants.focus()
    selectWithin(wants, 4, 10)
    await user.tab()
    expect(screen.getByRole('button', { name: 'Rewrite' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(bar()).toBeNull()
    expect(wants).toHaveFocus()
  })

  it('is raised by a keyboard selection, with no pointer involved', async () => {
    const user = userEvent.setup()
    show()
    const wants = proseOf('Wants')
    wants.focus()
    await user.keyboard('{Control>}a{/Control}')
    expect(bar()).not.toBeNull()
    const selection = window.getSelection()
    expect(selection?.toString()).toBe(wants.textContent)
  })

  it('extends a keyboard selection one whole character at a time', async () => {
    const user = userEvent.setup()
    show()
    const notes = proseOf('Notes')
    notes.focus()
    await user.keyboard('{Shift>}{ArrowRight}{ArrowRight}{/Shift}')
    expect(window.getSelection()?.toString()).toBe((notes.textContent ?? '').slice(0, 2))
  })

  it('goes away when the selection collapses', () => {
    show()
    const wants = proseOf('Wants')
    selectWithin(wants, 4, 10)
    expect(bar()).not.toBeNull()
    selectWithin(wants, 4, 4)
    expect(bar()).toBeNull()
  })
})

// ── AC 5 · the gold wash ─────────────────────────────────────────────────────

describe('the gold wash marks exactly the keys it was given (CANVAS-28)', () => {
  it('marks them, with a cue that is not colour, and acknowledges', async () => {
    const user = userEvent.setup()
    const onAcknowledgeChanges = vi.fn()
    show({ changedFields: ['wants'], onAcknowledgeChanges })
    expect(screen.getByText('Changed')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Got it' }))
    expect(onAcknowledgeChanges).toHaveBeenCalledTimes(1)
  })

  it('shows none for a hydrated document, whose version lists changed fields', () => {
    // CANVAS-28: the wash marks a change that arrived live in THIS client. A
    // reloaded document has a history and must still show nothing.
    const document = hydratedFixture()
    expect(document.version.changed_fields).toContain('wants')
    render(<GameDocument document={document} />)
    expect(screen.queryByText('Changed')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Got it' })).not.toBeInTheDocument()
  })

  it('ignores a key the type does not declare', () => {
    show({ changedFields: ['constructor', 'nonesuch'] })
    expect(screen.queryByText('Changed')).not.toBeInTheDocument()
  })
})

// ── AC 6 · X-7 and X-10 ──────────────────────────────────────────────────────

describe('nothing leaves the page', () => {
  it.each(DOCUMENT_TYPE_IDS)('%s loads no remote subresource (X-10)', (id) => {
    show({ typeId: id })
    for (const element of root().querySelectorAll('[src], [href], [srcset], [poster], [data]')) {
      for (const name of ['src', 'href', 'srcset', 'poster', 'data']) {
        const value = element.getAttribute(name)
        if (value === null) continue
        expect(value, `${id}: ${name}=${value}`).toMatch(/^\/campaigns\/[^/]+\/assets\/[^/?#]+$|^#/)
      }
    }
  })

  it('serves a portrait from the campaign asset route only', () => {
    show()
    expect(screen.getByRole('img', { name: /grey-robed woman/ })).toHaveAttribute(
      'src',
      '/campaigns/camp_vault_harbour/assets/ast_ondrey',
    )
  })

  it('writes nothing to web storage (X-7, CANVAS-15)', async () => {
    const user = userEvent.setup()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    show()
    await user.click(screen.getByRole('button', { name: 'Edit Voice' }))
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), ' and cold')
    await user.tab()
    expect(setItem).not.toHaveBeenCalled()
    setItem.mockRestore()
  })
})

// ── The empty document ───────────────────────────────────────────────────────

describe('an empty document', () => {
  it('invites editing without alarming anyone', () => {
    show({ empty: true })
    expect(screen.getAllByText('Nothing yet — press Edit to add it').length).toBeGreaterThan(3)
    expect(screen.getByRole('button', { name: 'Edit Voice' })).toBeInTheDocument()
  })

  it('still names every field the registry declares', () => {
    show({ empty: true })
    for (const rule of Object.values(REGISTRY.common_field_rules)) {
      expect(screen.getAllByText(rule.label).length).toBeGreaterThan(0)
    }
    for (const key of Object.keys(documentTypeById('npc')?.fields ?? {})) {
      const label = documentFieldReads('npc').find((field) => field.key === key)?.label ?? ''
      expect(screen.getAllByText(label).length, key).toBeGreaterThan(0)
    }
  })
})
