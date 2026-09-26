/**
 * GameDocument (agent-forge-harness-1kg.6.2) — one story per type, one per
 * state, and the interactions that only a real browser can answer.
 *
 * jsdom evaluates no CSS and has no layout, so the things these stories prove
 * are the ones the unit tests cannot: a pointer drag really selects text, the
 * bar is placed against a measured box and stays inside a 400 px canvas, the
 * structured editors are usable with nothing but a keyboard, and both themes
 * take every colour from a token.
 */

import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, waitFor, within } from 'storybook/test'

import { DOCUMENT_TYPE_IDS } from './contracts'
import type { Document, FieldValue } from './contracts'
import { documentFixture } from './documentFixtures'
import { GameDocument } from './GameDocument'
import type { GameDocumentProps } from './GameDocument'

/**
 * The shell is controlled, so a story that edits has to hold the document. This
 * is the smallest honest stand-in for `1kg.6.5`: it keeps the committed value
 * in memory and nowhere else (CANVAS-15, X-7), and it saves nothing.
 */
function LiveDocument(props: GameDocumentProps): React.JSX.Element {
  const [document, setDocument] = React.useState<Document>(props.document)
  return (
    <GameDocument
      {...props}
      document={document}
      onFieldCommit={(key: string, value: FieldValue) => {
        setDocument((was) => ({ ...was, data: { ...was.data, [key]: value } }))
        props.onFieldCommit?.(key, value)
      }}
    />
  )
}

const meta = {
  title: 'GM Workbench/GameDocument',
  component: GameDocument,
  parameters: { layout: 'padded' },
  args: {
    document: documentFixture('npc'),
    onFieldDraft: fn(),
    onFieldCommit: fn(),
    onRetry: fn(),
    onKeepMine: fn(),
    onUseLatest: fn(),
    onArmFieldEdit: fn(),
    onSelectionAction: fn(),
    onAcknowledgeChanges: fn(),
  },
  render: (args) => <LiveDocument {...args} />,
} satisfies Meta<typeof GameDocument>

export default meta
type Story = StoryObj<typeof meta>

// ── One story per type ───────────────────────────────────────────────────────

/**
 * Every label is the registry's, and no key or entity reaches the page.
 *
 * A Material Symbols ligature IS a snake_case word — `auto_fix_high` — but it
 * is decoration inside `aria-hidden="true"`, so nobody reads it. The sweep
 * skips hidden subtrees, which is also what makes it honest: a key that escaped
 * into a visible node is what it is looking for.
 */
async function assertNoRawKeys(canvasElement: HTMLElement): Promise<void> {
  const article = within(canvasElement).getByRole('article')
  const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = (node.textContent ?? '').trim()
    if (text === '' || node.parentElement?.closest('[aria-hidden="true"]') != null) continue
    await expect(text).not.toMatch(/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/)
    await expect(text).not.toMatch(/&[a-z]+;/)
  }
}

export const NpcDossier: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('NPC Dossier')).toBeInTheDocument()
    await expect(canvas.getByRole('group', { name: 'Wants' })).toBeInTheDocument()
    await expect(canvas.getByRole('img', { name: /grey-robed woman/ })).toBeInTheDocument()
    await assertNoRawKeys(canvasElement)
  },
}

export const StatBlock: Story = {
  args: { document: documentFixture('statblock') },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('article')).toHaveAttribute('data-renderer', 'stat_block_card')
    const abilities = canvas.getByRole('table', { name: 'Ability scores' })
    await expect(within(abilities).getByRole('row', { name: /Strength/ })).toHaveTextContent('+4')
    // The absent Intelligence score is absent, not 0.
    await expect(within(abilities).getByRole('row', { name: /Intelligence/ })).toHaveTextContent('Not set')
    await assertNoRawKeys(canvasElement)
  },
}

export const Handout: Story = {
  args: { document: documentFixture('handout') },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('article')).toHaveAttribute('data-printable', 'true')
    await assertNoRawKeys(canvasElement)
  },
}

export const SessionNotes: Story = {
  args: { document: documentFixture('session-notes') },
  play: async ({ canvasElement }) => {
    const present = within(canvasElement).getByRole('list', { name: 'Present' })
    await expect(within(present).getAllByRole('listitem')).toHaveLength(4)
    await assertNoRawKeys(canvasElement)
  },
}

export const QuestLog: Story = {
  args: { document: documentFixture('quest-log') },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('list', { name: 'Open threads' })).toBeInTheDocument()
    await assertNoRawKeys(canvasElement)
  },
}

export const CharacterSheet: Story = {
  args: { document: documentFixture('character-sheet') },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('table', { name: 'Ability scores' })).toBeInTheDocument()
    await assertNoRawKeys(canvasElement)
  },
}

export const LoreEntry: Story = {
  args: {
    document: documentFixture('lore'),
    sources: [
      {
        book: "Player's Handbook",
        chapter: null,
        section: 'Customs',
        entity: 'The Ninth Tide',
        page: 12,
        snippet: 'The harbour closes its vaults on the ninth.',
      },
    ],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('article')).toHaveAttribute('data-accent', 'arcane')
    await expect(canvas.getByText(/1 source/)).toBeInTheDocument()
    // X-10: a citation is plain text. Nothing in the document may fetch.
    for (const element of canvasElement.querySelectorAll('[src], [href]')) {
      const value = element.getAttribute('src') ?? element.getAttribute('href') ?? ''
      await expect(value).toMatch(/^\/campaigns\/|^#/)
    }
    await assertNoRawKeys(canvasElement)
  },
}

export const Encounter: Story = {
  args: { document: documentFixture('encounter') },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('list', { name: 'Combatants' })).toBeInTheDocument()
    await assertNoRawKeys(canvasElement)
  },
}

/** LIB-12: a hand-made document, every field empty but its name. */
export const EmptyDocument: Story = {
  args: { document: documentFixture('npc', { empty: true }) },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getAllByText('Nothing yet — press Edit to add it').length).toBeGreaterThan(3)
    await expect(canvas.getByRole('button', { name: 'Edit Voice' })).toBeInTheDocument()
    await expect(canvas.queryByRole('img')).toBeNull()
  },
}

/** X-8: a type from a newer server is a placeholder, never an NPC. */
export const UnknownType: Story = {
  args: { document: { ...documentFixture('npc'), type: DOCUMENT_TYPE_IDS[0] } },
  render: (args) => (
    <GameDocument {...args} document={{ ...args.document, type: 'spaceship' as (typeof DOCUMENT_TYPE_IDS)[number] }} />
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/newer version of Aetheril/)).toBeInTheDocument()
    await expect(canvas.queryByText('NPC Dossier')).toBeNull()
  },
}

// ── CANVAS-12 · one story per state ──────────────────────────────────────────

export const FieldSaving: Story = {
  args: { fieldStates: { voice: { state: 'saving' } } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Saving…')).toBeInTheDocument()
    // The text stays on screen while it saves.
    await expect(canvas.getByRole('textbox', { name: 'Voice' })).toHaveValue('Quiet, clipped, never raised')
    // STATE-7: a save in flight is not announced; the canvas header owns that.
    await expect(canvas.getByRole('status')).toHaveTextContent('')
  },
}

export const FieldSaved: Story = {
  args: { fieldStates: { voice: { state: 'saved' } } },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('status')).toHaveTextContent('Voice saved')
  },
}

/** CANVAS-14 + STATE-2: the error is attached, the text survives, Retry is real. */
export const FieldError: Story = {
  args: { fieldStates: { wants: { state: 'error', message: 'The server could not read that' } } },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('The server could not read that')).toBeInTheDocument()
    await expect(canvas.getByRole('textbox', { name: 'Wants' })).toHaveValue(
      String(documentFixture('npc').data.wants),
    )
    await userEvent.click(canvas.getByRole('button', { name: 'Retry saving Wants' }))
    await expect(args.onRetry).toHaveBeenCalledWith('wants')
  },
}

/** CANVAS-20: both values, and nothing is lost until the GM chooses. */
export const FieldConflict: Story = {
  args: {
    fieldStates: {
      wants: { state: 'conflict', latest: 'The signet, and the name struck through beside it.' },
    },
  },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const conflict = canvas.getByRole('group', { name: 'Wants changed elsewhere' })
    await expect(conflict).toHaveTextContent('The signet, and the name struck through beside it.')
    await userEvent.click(within(conflict).getByRole('button', { name: 'Keep mine' }))
    await expect(args.onKeepMine).toHaveBeenCalled()
    await userEvent.click(within(conflict).getByRole('button', { name: 'Use latest' }))
    await expect(args.onUseLatest).toHaveBeenCalledWith('wants')
  },
}

/** CANVAS-24: read-only here, editable everywhere else. */
export const AssistantIsEditing: Story = {
  args: { assistantEditing: ['wants'] },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Assistant is editing…')).toBeInTheDocument()
    await expect(canvas.queryByRole('button', { name: 'Edit Wants' })).toBeNull()
    await expect(canvas.getByRole('button', { name: 'Edit Voice' })).toBeInTheDocument()
  },
}

/** CANVAS-28 and CANVAS-29: a written cue, not colour alone, and **Got it**. */
export const GoldWash: Story = {
  args: { changedFields: ['wants', 'leverage'] },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getAllByText('Changed')).toHaveLength(2)
    await userEvent.click(canvas.getByRole('button', { name: 'Got it' }))
    await expect(args.onAcknowledgeChanges).toHaveBeenCalledTimes(1)
  },
}

/** REVEAL-13: the badge and the eye marker are the server's words, passed in. */
export const RevealedToTheTable: Story = {
  args: { revealBadge: 'REVEALED', revealedFields: ['portrait', 'voice'] },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('REVEALED')).toBeInTheDocument()
    await expect(canvas.getAllByText('The table can see this')).toHaveLength(2)
  },
}

/** REVEAL-13's third case: a client that cannot confirm never says "hidden". */
export const RevealStateUnknown: Story = {
  args: { revealBadge: 'Reveal state unknown — reconnecting' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    await expect(canvas.queryByText('GM ONLY')).toBeNull()
  },
}

/**
 * The same case reached the other way, which is the one that was wrong: an
 * owner writing `revealBadge={projection?.badge ?? null}` while the projection
 * has dropped. An explicit `null` is "could not confirm", not "nothing
 * revealed".
 */
export const RevealStateUnconfirmed: Story = {
  args: { revealBadge: null },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    await expect(canvas.queryByText('GM ONLY')).toBeNull()
  },
}

/** And the default the owner keeps: an omitted prop is a document nobody has
 * revealed, and `GM ONLY` is the true thing to say about one. */
export const GmOnlyByDefault: Story = {
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('GM ONLY')).toBeInTheDocument()
  },
}

// ── Keyboard editing, one structured kind at a time ──────────────────────────

/** A `text_list` edited with nothing but a keyboard. */
export const KeyboardEditsAList: Story = {
  args: { document: documentFixture('session-notes') },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Edit Present' }))
    const second = canvas.getByRole('textbox', { name: 'Present 2' })
    await waitFor(() => expect(canvas.getByRole('textbox', { name: 'Present 1' })).toHaveFocus())

    await userEvent.clear(second)
    await userEvent.type(second, 'Idris Q')
    await userEvent.tab()
    await expect(args.onFieldCommit).toHaveBeenCalledWith('present', ['Bess', 'Idris Q', 'Marlow', 'Quen'])

    await userEvent.click(canvas.getByRole('button', { name: 'Move Present 2 up' }))
    await expect(args.onFieldCommit).toHaveBeenLastCalledWith('present', ['Idris Q', 'Bess', 'Marlow', 'Quen'])
    await waitFor(() => expect(canvas.getByRole('textbox', { name: 'Present 1' })).toHaveFocus())
  },
}

/** An `abilities` block: six labelled boxes, modifiers derived, bounds enforced. */
export const KeyboardEditsAbilityScores: Story = {
  args: { document: documentFixture('character-sheet') },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Edit Ability scores' }))
    const strength = canvas.getByRole('spinbutton', { name: 'Strength' })
    await userEvent.clear(strength)
    await userEvent.type(strength, '100')
    await userEvent.tab()
    await expect(canvas.getByText('Between 0 and 99')).toBeInTheDocument()
    await expect(args.onFieldCommit).not.toHaveBeenCalled()

    await userEvent.clear(canvas.getByRole('spinbutton', { name: 'Strength' }))
    await userEvent.type(canvas.getByRole('spinbutton', { name: 'Strength' }), '16')
    await userEvent.tab()
    await expect(args.onFieldCommit).toHaveBeenCalledWith('abilities', {
      str: 16,
      dex: 17,
      con: 14,
      int: 11,
      wis: 15,
      cha: 8,
    })
  },
}

/** An `entry_list`: a name and a text, kept apart. */
export const KeyboardEditsStatBlockEntries: Story = {
  args: { document: documentFixture('statblock') },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Edit Reactions' }))
    const text = canvas.getByRole('textbox', { name: 'Reactions 1 text' })
    await userEvent.clear(text)
    await userEvent.type(text, 'The water rises with it.')
    await userEvent.tab()
    await expect(args.onFieldCommit).toHaveBeenCalledWith('reactions', [
      { name: 'Answering Tide', text: 'The water rises with it.' },
    ])
  },
}

// ── CANVAS-23 · the SelectionBar, with a real layout ─────────────────────────

function proseIn(canvasElement: HTMLElement, label: string): HTMLElement {
  return within(canvasElement).getByRole('group', { name: label })
}

/** A pointer drag across part of one prose field. */
export const SelectionByPointer: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const wants = proseIn(canvasElement, 'Wants')
    await userEvent.pointer([
      { target: wants, offset: 4, keys: '[MouseLeft>]' },
      { target: wants, offset: 14 },
      { keys: '[/MouseLeft]' },
    ])
    const bar = await canvas.findByRole('toolbar', { name: 'Ask the assistant about the selected text in Wants' })
    // It is placed against a measured box, and it does not cover the words.
    const barBox = bar.getBoundingClientRect()
    await expect(barBox.width).toBeGreaterThan(0)

    await userEvent.click(within(bar).getByRole('button', { name: 'Shorter' }))
    await expect(args.onSelectionAction).toHaveBeenCalledWith(
      'shorter',
      expect.objectContaining({ field: 'wants', start: 4, end: 14 }),
    )
  },
}

/** CANVAS-23: a selection across two fields raises nothing. */
export const SelectionAcrossFieldsRaisesNothing: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.pointer([
      { target: proseIn(canvasElement, 'Wants'), offset: 4, keys: '[MouseLeft>]' },
      { target: proseIn(canvasElement, 'Leverage'), offset: 10 },
      { keys: '[/MouseLeft]' },
    ])
    await expect(canvas.queryByRole('toolbar', { name: /selected text/ })).toBeNull()
    await expect(args.onSelectionAction).not.toHaveBeenCalled()
  },
}

/**
 * The same bar, with no pointer at all: focus the text, select it with
 * Ctrl/Cmd+A, Tab into the bar, act, and Escape back out.
 */
export const SelectionByKeyboard: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const wants = proseIn(canvasElement, 'Wants')
    wants.focus()
    await expect(wants).toHaveFocus()

    await userEvent.keyboard('{Control>}a{/Control}')
    const bar = await canvas.findByRole('toolbar', { name: /selected text in Wants/ })

    await userEvent.tab()
    await expect(within(bar).getByRole('button', { name: 'Rewrite' })).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    await expect(args.onSelectionAction).toHaveBeenCalledWith('rewrite', expect.objectContaining({ field: 'wants' }))

    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvas.queryByRole('toolbar', { name: /selected text/ })).toBeNull())
    await expect(wants).toHaveFocus()
  },
}

/** Selecting text is not editing it (CANVAS-23). */
export const SelectingDoesNotEnterEditMode: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const wants = proseIn(canvasElement, 'Wants')
    await userEvent.pointer([
      { target: wants, offset: 0, keys: '[MouseLeft>]' },
      { target: wants, offset: 12 },
      { keys: '[/MouseLeft]' },
    ])
    await canvas.findByRole('toolbar', { name: /selected text/ })
    await expect(canvas.queryByRole('textbox', { name: 'Wants' })).toBeNull()
  },
}

// ── §10.2 · a narrow canvas, and the dark theme ──────────────────────────────

/** The bar stays inside a 400 px canvas, which is the narrow layout's width. */
export const NarrowCanvas: Story = {
  decorators: [
    (Story) => (
      <div style={{ width: 400, overflow: 'hidden' }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const wants = proseIn(canvasElement, 'Wants')
    await userEvent.pointer([
      { target: wants, offset: 20, keys: '[MouseLeft>]' },
      { target: wants, offset: 40 },
      { keys: '[/MouseLeft]' },
    ])
    const bar = await canvas.findByRole('toolbar', { name: /selected text/ })
    const pane = canvas.getByRole('article').getBoundingClientRect()
    const box = bar.getBoundingClientRect()
    await expect(box.left).toBeGreaterThanOrEqual(pane.left - 1)
    await expect(box.right).toBeLessThanOrEqual(pane.right + 1)
  },
}

export const NarrowStatBlock: Story = {
  args: { document: documentFixture('statblock') },
  decorators: [
    (Story) => (
      <div style={{ width: 400, overflow: 'hidden' }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const article = within(canvasElement).getByRole('article')
    await expect(article.scrollWidth).toBeLessThanOrEqual(article.clientWidth + 1)
  },
}

export const DarkTheme: Story = {
  globals: { theme: 'dark' },
  args: { changedFields: ['wants'], fieldStates: { voice: { state: 'saved' } } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Changed')).toBeInTheDocument()
    // Tokens only: the wash must have resolved to a real colour in this theme.
    const washed = canvasElement.querySelector('[data-changed="true"]')
    await expect(washed).not.toBeNull()
    if (washed !== null) {
      await expect(getComputedStyle(washed).backgroundColor).not.toBe('rgba(0, 0, 0, 0)')
    }
  },
}

export const DarkStatBlock: Story = {
  globals: { theme: 'dark' },
  args: { document: documentFixture('statblock') },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('table', { name: 'Ability scores' })).toBeInTheDocument()
  },
}
