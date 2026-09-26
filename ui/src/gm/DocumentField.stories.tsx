/**
 * DocumentField — one labelled native control per field kind, and CANVAS-12's
 * six states.
 *
 * It had unit tests and no stories, which for this component is the wrong way
 * round: the kinds differ mostly in what they LOOK like, and the six states
 * (clean, editing, saving, saved, error, conflict) are pure presentation. The
 * fixtures are the registry's own, so a field's label and kind here are the
 * ones the product ships.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { tabTo } from '../../.storybook/keyboard'
import { DocumentField } from './DocumentField'
import type { DocumentTypeId, FieldValue } from './contracts'
import { documentFieldReads } from './documentFields'
import type { DocumentFieldRead } from './documentFields'
import { documentFixture } from './documentFixtures'

function fieldRead(key: string, typeId: DocumentTypeId = 'npc'): DocumentFieldRead {
  const read = documentFieldReads(typeId).find((field) => field.key === key)
  if (read === undefined) throw new Error(`${typeId} declares no field ${key}`)
  return read
}

function fixtureValue(key: string, typeId: DocumentTypeId = 'npc'): FieldValue | undefined {
  return documentFixture(typeId).data[key]
}

const NPC = documentFixture('npc')

const meta = {
  title: 'GM/DocumentField',
  component: DocumentField,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    field: fieldRead('voice'),
    value: fixtureValue('voice'),
    documentName: String(NPC.data.name),
    campaignId: NPC.campaign_id,
    onDraft: fn(),
    onCommit: fn(),
    onRetry: fn(),
    onKeepMine: fn(),
    onUseLatest: fn(),
    onArmEdit: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ maxWidth: 560 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DocumentField>

export default meta
type Story = StoryObj<typeof meta>

// ── Kinds ────────────────────────────────────────────────────────────────────

/** `text` — a single line. */
export const TextField: Story = {}

/** `prose` — a paragraph. */
export const ProseField: Story = {
  args: { field: fieldRead('if_attacked'), value: fixtureValue('if_attacked') },
}

/** `text_list` — items stay items; the handoff flattened them to a CSV string. */
export const TextListField: Story = {
  args: { field: fieldRead('tags'), value: fixtureValue('tags') },
}

/** `integer` — Armour Class on a stat block. */
export const IntegerField: Story = {
  args: {
    field: fieldRead('ac', 'statblock'),
    value: fixtureValue('ac', 'statblock'),
    documentName: String(documentFixture('statblock').data.name),
  },
}

/** `abilities` — six scores, each its own control. */
export const AbilitiesField: Story = {
  args: {
    field: fieldRead('abilities', 'statblock'),
    value: fixtureValue('abilities', 'statblock'),
    documentName: String(documentFixture('statblock').data.name),
  },
}

/** A key the document has no value for: a placeholder that invites an edit. */
export const EmptyValue: Story = {
  args: { field: fieldRead('notes'), value: undefined },
}

/** Text long enough to wrap several times. */
export const LongValue: Story = {
  args: {
    field: fieldRead('if_attacked'),
    value:
      'She does not fight. She puts the counter between herself and the door, keeps talking in the same flat voice, and lets them wreck the place — and every name they say while they do it goes into the ledger under the bar. ' +
      'By morning three people who were never in the room will know exactly who came in and what they wanted.',
  },
}

// ── CANVAS-12's six states ───────────────────────────────────────────────────

export const Saving: Story = {
  args: { status: { state: 'saving' } },
}

export const Saved: Story = {
  args: { status: { state: 'saved' } },
}

/** CANVAS-14: the server's message is attached to the field it refused. */
export const SaveFailed: Story = {
  args: { status: { state: 'error', message: 'The campaign is read-only right now.' } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('The campaign is read-only right now.')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: /retry|try again/i })).toBeInTheDocument()
  },
}

/** CANVAS-20: someone else wrote first, and both versions are offered. */
export const Conflict: Story = {
  args: {
    status: {
      state: 'conflict',
      latest: 'Flat, unhurried, and never once above the noise of the room.',
    },
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/never once above the noise/)).toBeInTheDocument()
  },
}

/** CANVAS-24: the assistant holds this field, so the GM does not also edit it. */
export const AssistantEditing: Story = {
  args: { assistantEditing: true },
}

/** CANVAS-28: a value that arrived live in this client is washed gold. */
export const RecentlyChanged: Story = {
  args: { changed: true },
}

/** REVEAL-13: the table can see this field right now. */
export const Revealed: Story = {
  args: { revealed: true },
}

// ── Keyboard ─────────────────────────────────────────────────────────────────

/**
 * Edited by keyboard alone: the Edit control is reachable, Enter opens the
 * editor, the control takes focus, and the edited value reaches `onCommit`.
 *
 * "Reachable" means reached with Tab presses (`tabTo`) — a scripted `.focus()`
 * works on a control the tab order has lost, which is the failure this half of
 * the claim exists to catch.
 */
export const EditedByKeyboard: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const edit = canvas.getByRole('button', { name: 'Edit Voice' })
    await tabTo(edit)
    await userEvent.keyboard('{Enter}')

    const control = canvas.getByRole('textbox', { name: 'Voice' })
    await expect(control).toHaveFocus()
    await userEvent.keyboard('{Control>}a{/Control}Gravel and old smoke')
    await expect(args.onDraft).toHaveBeenCalled()

    await userEvent.tab()
    await expect(args.onCommit).toHaveBeenCalledWith('voice', 'Gravel and old smoke')
  },
}

/** `asset` — an image reference, not text; the editor is a different control. */
export const AssetField: Story = {
  args: { field: fieldRead('portrait'), value: fixtureValue('portrait') },
}

// ── Dark ─────────────────────────────────────────────────────────────────────

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkSaveFailed: Story = {
  globals: { theme: 'dark' },
  args: { status: { state: 'error', message: 'The campaign is read-only right now.' } },
}

export const DarkConflict: Story = {
  globals: { theme: 'dark' },
  args: {
    status: {
      state: 'conflict',
      latest: 'Flat, unhurried, and never once above the noise of the room.',
    },
  },
}

export const DarkAbilities: Story = {
  globals: { theme: 'dark' },
  args: {
    field: fieldRead('abilities', 'statblock'),
    value: fixtureValue('abilities', 'statblock'),
    documentName: String(documentFixture('statblock').data.name),
  },
}
