/**
 * DocumentField (agent-forge-harness-1kg.6.2) — one labelled native control per
 * field kind, and CANVAS-12's six states.
 *
 * Every query here is by ROLE AND NAME. A field whose control can only be found
 * by a test id, a class or its position is a field a screen reader cannot use,
 * so the tests are written the way the GM's assistive technology reads the page.
 *
 * The structured kinds get the most attention, because flattening one to a
 * string is the defect the handoff shipped (`text_list` joined with commas,
 * `contentEditable` on everything): each of them asserts on the VALUE that
 * reaches the commit callback, not on what is on screen.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ABILITY_SCORE_MAX, INTEGER_FIELD_MAX, type DocumentTypeId, type FieldValue } from './contracts'
import { documentFieldReads, type DocumentFieldRead, type FieldStatus } from './documentFields'
import { documentFixture } from './documentFixtures'
import { DocumentField } from './DocumentField'

function fieldRead(key: string, typeId: DocumentTypeId = 'npc'): DocumentFieldRead {
  const read = documentFieldReads(typeId).find((field) => field.key === key)
  if (read === undefined) throw new Error(`${typeId} declares no field ${key}`)
  return read
}

interface Options {
  typeId?: DocumentTypeId
  value?: FieldValue
  status?: FieldStatus
  assistantEditing?: boolean
  changed?: boolean
  revealed?: boolean
  onDraft?: (key: string, value: FieldValue) => void
  onCommit?: (key: string, value: FieldValue) => void
  onRetry?: (key: string) => void
  onKeepMine?: (key: string, value: FieldValue) => void
  onUseLatest?: (key: string) => void
  onArmEdit?: (key: string) => void
}

function show(key: string, options: Options = {}) {
  const { typeId = 'npc', value, ...rest } = options
  const document = documentFixture(typeId)
  const read = fieldRead(key, typeId)
  const result = render(
    <DocumentField
      field={read}
      value={value === undefined ? document.data[key] : value}
      documentName={String(document.data.name)}
      campaignId={document.campaign_id}
      {...rest}
    />,
  )
  return { ...result, read }
}

/** The Edit control, then the control the editor put on screen. */
async function openEditor(user: ReturnType<typeof userEvent.setup>, label: string): Promise<void> {
  await user.click(screen.getByRole('button', { name: `Edit ${label}` }))
}

// ── Labels and the read presentation ─────────────────────────────────────────

describe('every field is named by the registry, never by its key', () => {
  it('names the read presentation with the registry label', () => {
    show('if_attacked')
    expect(screen.getByRole('group', { name: 'If the party attacks' })).toBeInTheDocument()
  })

  it('renders a bare ampersand as itself, never as an entity', () => {
    show('history', { typeId: 'lore' })
    const group = screen.getByRole('group', { name: 'History' })
    expect(group.textContent).toContain('Board & older')
    expect(group.textContent).not.toMatch(/&[a-z]+;/)
  })

  it('shows a quiet placeholder that invites editing when the field is empty and editable', () => {
    show('voice', { value: '' })
    expect(screen.getByRole('group', { name: 'Voice' })).toHaveTextContent('Nothing yet')
    expect(screen.getByRole('button', { name: 'Edit Voice' })).toBeInTheDocument()
  })

  it('says nothing alarming for an empty field that cannot be edited', () => {
    const read: DocumentFieldRead = { ...fieldRead('voice'), editable: false }
    render(<DocumentField field={read} value="" documentName="Ondrey" campaignId="camp_1" />)
    expect(screen.getByRole('group', { name: 'Voice' })).toHaveTextContent('Not set')
    expect(screen.queryByRole('button', { name: 'Edit Voice' })).not.toBeInTheDocument()
  })

  it('renders a labelled fallback for a kind it does not know, and no raw key', () => {
    const read: DocumentFieldRead = { key: 'sigil', kind: 'rune_grid', label: 'Sigil', editable: true }
    render(<DocumentField field={read} value="a value" documentName="Ondrey" campaignId="camp_1" />)
    const group = screen.getByRole('group', { name: 'Sigil' })
    expect(group).toHaveTextContent('a value')
    expect(group.textContent).not.toContain('sigil')
    expect(group.textContent).not.toContain('rune_grid')
  })
})

// ── text and prose ───────────────────────────────────────────────────────────

describe('text — one labelled line', () => {
  it('opens a named textbox, reports each keystroke and commits on blur', async () => {
    const user = userEvent.setup()
    const onDraft = vi.fn()
    const onCommit = vi.fn()
    show('voice', { value: 'Quiet', onDraft, onCommit })

    await openEditor(user, 'Voice')
    const box = screen.getByRole('textbox', { name: 'Voice' })
    await user.clear(box)
    await user.type(box, 'Clipped')
    expect(onDraft).toHaveBeenLastCalledWith('voice', 'Clipped')
    expect(onCommit).not.toHaveBeenCalled()

    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('voice', 'Clipped')
  })

  it('commits on Enter without leaving the field empty', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('voice', { value: 'Quiet', onCommit })
    await openEditor(user, 'Voice')
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), '!{Enter}')
    expect(onCommit).toHaveBeenCalledWith('voice', 'Quiet!')
  })

  it('keeps the GM out of the editor when Escape is pressed, without committing', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('voice', { value: 'Quiet', onCommit })
    await openEditor(user, 'Voice')
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), ' and low{Escape}')
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByRole('group', { name: 'Voice' })).toHaveTextContent('Quiet')
  })

  it('refuses text past the bound the SERVER counts, and keeps what was typed', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('voice', { value: 'x'.repeat(200), onCommit })
    await openEditor(user, 'Voice')
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), 'y')
    await user.tab()
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Voice' })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('At most 200 characters')).toBeInTheDocument()
  })

  it('still lets the GM edit 150 dice, which a UTF-16 maxLength would have frozen', async () => {
    // 150 astral characters are 300 UTF-16 units. A `maxLength` of 200 would
    // refuse every keystroke in a field the server is perfectly happy with.
    const user = userEvent.setup()
    const onCommit = vi.fn()
    const dice = '\u{1F3B2}'.repeat(150)
    show('voice', { value: dice, onCommit })
    await openEditor(user, 'Voice')
    await user.type(screen.getByRole('textbox', { name: 'Voice' }), '!')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('voice', `${dice}!`)
  })
})

describe('prose — a multi-line control, and Ctrl+S', () => {
  it('commits on Ctrl+S without leaving the editor', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('wants', { value: 'The signet', onCommit })
    await openEditor(user, 'Wants')
    const box = screen.getByRole('textbox', { name: 'Wants' })
    await user.type(box, '.')
    await user.keyboard('{Control>}s{/Control}')
    expect(onCommit).toHaveBeenCalledWith('wants', 'The signet.')
  })

  it('keeps newlines rather than collapsing them into one line', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('wants', { value: 'One', onCommit })
    await openEditor(user, 'Wants')
    await user.type(screen.getByRole('textbox', { name: 'Wants' }), '{Enter}Two')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('wants', 'One\nTwo')
  })
})

// ── text_list ────────────────────────────────────────────────────────────────

describe('text_list — rows, never one comma-joined string', () => {
  it('reads as a list of items', () => {
    show('present', { typeId: 'session-notes' })
    const list = screen.getByRole('list', { name: 'Present' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(4)
    expect(list.textContent).not.toContain('Bess, Idris')
  })

  it('gives every row its own named box and commits an array', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('present', { typeId: 'session-notes', onCommit })
    await openEditor(user, 'Present')
    const second = screen.getByRole('textbox', { name: 'Present 2' })
    await user.clear(second)
    await user.type(second, 'Idris Q')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('present', ['Bess', 'Idris Q', 'Marlow', 'Quen'])
  })

  it('reorders and removes from the keyboard, each one a structured commit', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('present', { typeId: 'session-notes', onCommit })
    await openEditor(user, 'Present')

    await user.click(screen.getByRole('button', { name: 'Move Present 2 up' }))
    expect(onCommit).toHaveBeenLastCalledWith('present', ['Idris', 'Bess', 'Marlow', 'Quen'])

    await user.click(screen.getByRole('button', { name: 'Move Present 1 down' }))
    expect(onCommit).toHaveBeenLastCalledWith('present', ['Bess', 'Idris', 'Marlow', 'Quen'])

    await user.click(screen.getByRole('button', { name: 'Remove Present 4' }))
    expect(onCommit).toHaveBeenLastCalledWith('present', ['Bess', 'Idris', 'Marlow'])
  })

  it('adds an empty row as a draft, focuses it, and does not save a blank item', async () => {
    // The wire says an item is 1 to 2,000 characters, so an empty row is not
    // yet an item: Add opens one, and a row left blank is dropped on commit.
    const user = userEvent.setup()
    const onDraft = vi.fn()
    const onCommit = vi.fn()
    show('present', { typeId: 'session-notes', onDraft, onCommit })
    await openEditor(user, 'Present')

    await user.click(screen.getByRole('button', { name: 'Add to Present' }))
    expect(onDraft).toHaveBeenLastCalledWith('present', ['Bess', 'Idris', 'Marlow', 'Quen', ''])
    expect(onCommit).not.toHaveBeenCalledWith('present', expect.arrayContaining(['']))
    expect(screen.getByRole('textbox', { name: 'Present 5' })).toHaveFocus()

    await user.tab()
    expect(onCommit).toHaveBeenLastCalledWith('present', ['Bess', 'Idris', 'Marlow', 'Quen'])
  })

  it('moves the focus with the row it moved, not with the position', async () => {
    const user = userEvent.setup()
    show('present', { typeId: 'session-notes' })
    await openEditor(user, 'Present')
    await user.click(screen.getByRole('button', { name: 'Move Present 2 up' }))
    const moved = screen.getByRole('textbox', { name: 'Present 1' })
    expect(moved).toHaveValue('Idris')
    expect(moved).toHaveFocus()
  })

  it('cannot move the first row up or the last row down', async () => {
    const user = userEvent.setup()
    show('present', { typeId: 'session-notes' })
    await openEditor(user, 'Present')
    expect(screen.getByRole('button', { name: 'Move Present 1 up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Present 4 down' })).toBeDisabled()
  })
})

// ── entry_list ───────────────────────────────────────────────────────────────

describe('entry_list — a name and a text, both kept', () => {
  it('reads each entry with its name and its text', () => {
    show('actions', { typeId: 'statblock' })
    const list = screen.getByRole('list', { name: 'Actions' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(list).toHaveTextContent('Multiattack')
    expect(list).toHaveTextContent('It makes two brine-lash attacks.')
  })

  it('edits a name without touching the text, and commits entries', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('actions', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Actions')
    const name = screen.getByRole('textbox', { name: 'Actions 1 name' })
    await user.clear(name)
    await user.type(name, 'Multiattack (2)')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('actions', [
      { name: 'Multiattack (2)', text: 'It makes two brine-lash attacks.' },
      {
        name: 'Brine Lash',
        text: 'Melee weapon attack, +7 to hit, reach 10 ft. Hit: 12 (2d8 + 3) bludgeoning damage.',
      },
    ])
  })

  it('adds an empty entry rather than a string', async () => {
    const user = userEvent.setup()
    const onDraft = vi.fn()
    show('reactions', { typeId: 'statblock', onDraft })
    await openEditor(user, 'Reactions')
    await user.click(screen.getByRole('button', { name: 'Add to Reactions' }))
    expect(onDraft).toHaveBeenLastCalledWith('reactions', [
      { name: 'Answering Tide', text: 'When struck, it rises and the water rises with it.' },
      { name: '', text: '' },
    ])
  })

  it('refuses an entry that has a text and no name', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('reactions', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Reactions')
    await user.clear(screen.getByRole('textbox', { name: 'Reactions 1 name' }))
    await user.tab()
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByText('Every entry needs a name')).toBeInTheDocument()
  })
})

// ── abilities ────────────────────────────────────────────────────────────────

describe('abilities — six scores, derived modifiers, and an absent score is not 0', () => {
  it('spells every score out and derives its modifier', () => {
    show('abilities', { typeId: 'statblock' })
    const table = screen.getByRole('table', { name: 'Ability scores' })
    expect(within(table).getByRole('row', { name: /Strength/ })).toHaveTextContent('18')
    expect(within(table).getByRole('row', { name: /Strength/ })).toHaveTextContent('+4')
  })

  it('shows an unset score as unset, never as zero', () => {
    show('abilities', { typeId: 'statblock' })
    const table = screen.getByRole('table', { name: 'Ability scores' })
    const intelligence = within(table).getByRole('row', { name: /Intelligence/ })
    expect(intelligence).toHaveTextContent('Not set')
    expect(intelligence).not.toHaveTextContent('0')
  })

  it('edits one score and commits the block as an object', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('abilities', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Ability scores')
    const strength = screen.getByRole('spinbutton', { name: 'Strength' })
    await user.clear(strength)
    await user.type(strength, '20')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('abilities', {
      str: 20,
      dex: 12,
      con: 17,
      int: null,
      wis: 13,
      cha: 16,
    })
  })

  it('refuses a score outside the contract bound and keeps the typed text', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('abilities', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Ability scores')
    const strength = screen.getByRole('spinbutton', { name: 'Strength' })
    await user.clear(strength)
    await user.type(strength, String(ABILITY_SCORE_MAX + 1))
    await user.tab()
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByText('Between 0 and 99')).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'Strength' })).toHaveValue(ABILITY_SCORE_MAX + 1)
  })
})

// ── integer ──────────────────────────────────────────────────────────────────

describe('integer — a number box with the contract bounds', () => {
  it('commits a number, not a string', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('hp', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Hit Points')
    const box = screen.getByRole('spinbutton', { name: 'Hit Points' })
    await user.clear(box)
    await user.type(box, '120')
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('hp', 120)
  })

  it('clears to null when the box is emptied', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('hp', { typeId: 'statblock', onCommit })
    await openEditor(user, 'Hit Points')
    await user.clear(screen.getByRole('spinbutton', { name: 'Hit Points' }))
    await user.tab()
    expect(onCommit).toHaveBeenCalledWith('hp', null)
  })

  it('carries the bound as a real constraint on the control', async () => {
    const user = userEvent.setup()
    show('hp', { typeId: 'statblock' })
    await openEditor(user, 'Hit Points')
    expect(screen.getByRole('spinbutton', { name: 'Hit Points' })).toHaveAttribute('max', String(INTEGER_FIELD_MAX))
  })
})

// ── asset ────────────────────────────────────────────────────────────────────

describe('asset — the campaign asset route, and never a remote URL', () => {
  it('renders the portrait through the route the contract names', () => {
    show('portrait')
    const image = screen.getByRole('img', { name: /grey-robed woman/ })
    expect(image).toHaveAttribute('src', '/campaigns/camp_vault_harbour/assets/ast_ondrey')
  })

  it('falls back to the label and the document name when the reference has no alt', () => {
    show('portrait', { value: { asset_id: 'ast_x', media_type: 'image', alt: ' ' } })
    expect(screen.getByRole('img', { name: 'Portrait of Sister Ondrey Vashe' })).toBeInTheDocument()
  })

  it('clears the field rather than offering an upload this bead cannot do', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    show('portrait', { onCommit })
    await user.click(screen.getByRole('button', { name: 'Remove Portrait' }))
    expect(onCommit).toHaveBeenCalledWith('portrait', null)
  })

  it('offers no Remove and no broken image when there is no portrait', () => {
    show('portrait', { value: null })
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove Portrait' })).not.toBeInTheDocument()
  })
})

// ── CANVAS-12: the six states ────────────────────────────────────────────────

describe('the six field states', () => {
  it('is quiet when it is clean', () => {
    show('voice', { status: { state: 'clean' } })
    expect(screen.queryByText('Saving…')).not.toBeInTheDocument()
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  })

  it('says it is saving, and keeps the text on screen', () => {
    show('voice', { status: { state: 'saving' } })
    expect(screen.getByText('Saving…')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Voice' })).toHaveValue('Quiet, clipped, never raised')
  })

  it('says it saved', () => {
    show('voice', { status: { state: 'saved' } })
    expect(screen.getByText('Saved')).toBeInTheDocument()
  })

  it('attaches an error to its field, keeps the text and offers Retry (CANVAS-14, STATE-2)', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    show('voice', { status: { state: 'error', message: 'Voice is too long' }, onRetry })
    expect(screen.getByRole('textbox', { name: 'Voice' })).toHaveValue('Quiet, clipped, never raised')
    expect(screen.getByText('Voice is too long')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry saving Voice' }))
    expect(onRetry).toHaveBeenCalledWith('voice')
  })

  it('shows both values on a conflict, and loses neither (CANVAS-20)', async () => {
    const user = userEvent.setup()
    const onKeepMine = vi.fn()
    const onUseLatest = vi.fn()
    show('voice', {
      status: { state: 'conflict', latest: 'Loud, and getting louder' },
      onKeepMine,
      onUseLatest,
    })
    const conflict = screen.getByRole('group', { name: 'Voice changed elsewhere' })
    expect(conflict).toHaveTextContent('Quiet, clipped, never raised')
    expect(conflict).toHaveTextContent('Loud, and getting louder')

    await user.click(within(conflict).getByRole('button', { name: 'Keep mine' }))
    expect(onKeepMine).toHaveBeenCalledWith('voice', 'Quiet, clipped, never raised')
    await user.click(within(conflict).getByRole('button', { name: 'Use latest' }))
    expect(onUseLatest).toHaveBeenCalledWith('voice')
  })

  it('compares a structured value without flattening either side', () => {
    show('present', {
      typeId: 'session-notes',
      status: { state: 'conflict', latest: ['Bess', 'Quen'] },
    })
    const conflict = screen.getByRole('group', { name: 'Present changed elsewhere' })
    expect(within(conflict).getAllByRole('list')).toHaveLength(2)
  })
})

// ── CANVAS-24 ────────────────────────────────────────────────────────────────

describe('while the assistant holds the field', () => {
  it('says so, and offers no way to type into it', () => {
    show('wants', { assistantEditing: true })
    expect(screen.getByText('Assistant is editing…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit Wants' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Wants' })).not.toBeInTheDocument()
  })
})

// ── CANVAS-28 ────────────────────────────────────────────────────────────────

describe('the gold wash', () => {
  it('carries a cue that is not colour', () => {
    show('wants', { changed: true })
    expect(screen.getByText('Changed')).toBeInTheDocument()
  })

  it('marks nothing when the field was not named as changed', () => {
    show('wants', { changed: false })
    expect(screen.queryByText('Changed')).not.toBeInTheDocument()
  })
})

// ── CANVAS-21 · the field-scope arming gesture ───────────────────────────────

describe('arming an AI edit for one field', () => {
  it('offers the control only when the owner wired it, and only reports', async () => {
    const user = userEvent.setup()
    const onArmEdit = vi.fn()
    show('wants', { onArmEdit })
    await user.click(screen.getByRole('button', { name: 'Edit Wants with assistant' }))
    expect(onArmEdit).toHaveBeenCalledWith('wants')
  })

  it('is absent when nothing is wired to it', () => {
    show('wants')
    expect(screen.queryByRole('button', { name: 'Edit Wants with assistant' })).not.toBeInTheDocument()
  })
})

// ── REVEAL-13 ────────────────────────────────────────────────────────────────

describe('the reveal marker', () => {
  it('shows what the owner passed in, and computes nothing', () => {
    show('voice', { revealed: true })
    expect(screen.getByText('The table can see this')).toBeInTheDocument()
  })

  it('shows nothing by itself', () => {
    show('voice')
    expect(screen.queryByText('The table can see this')).not.toBeInTheDocument()
  })
})

// ── X-7 ──────────────────────────────────────────────────────────────────────

describe('GM-private text never leaves the page', () => {
  it('writes nothing to web storage while the GM types', async () => {
    const user = userEvent.setup()
    const local = vi.spyOn(Storage.prototype, 'setItem')
    show('wants', { value: 'The signet' })
    await openEditor(user, 'Wants')
    await user.type(screen.getByRole('textbox', { name: 'Wants' }), ' and the ledger')
    await user.tab()
    expect(local).not.toHaveBeenCalled()
    local.mockRestore()
  })
})
