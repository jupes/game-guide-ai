/**
 * LeftNav — mode chips, the conversation list, and the UserMenu.
 *
 * The conversation list is the part with states: it can be empty, it can be
 * long, one row can be selected, and one row can be in rename mode.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { LeftNav } from './LeftNav'

// Kept under the 40-code-point title cap, so the row labels are the prompts.
const SAGE_THREAD = [
  { mode: 'sage' as const, firstPrompt: 'Shield spell: what does it stop?' },
  { mode: 'sage' as const, firstPrompt: 'Invisibility after attacking?' },
  { mode: 'sage' as const, firstPrompt: 'Familiar touch spell through a wall?' },
]

const meta = {
  title: 'Shell/LeftNav',
  component: LeftNav,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ conversations: SAGE_THREAD, selected: 1 })],
} satisfies Meta<typeof LeftNav>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/**
 * A fresh account. The header and the New button stay; only the rows are gone,
 * so there is always somewhere to start.
 */
export const NoConversations: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Conversations')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'New conversation' })).toBeEnabled()
  },
}

/** The open row is the pressed one, which is what a screen reader reads out. */
export const SelectedConversation: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const selected = canvas.getByRole('button', {
      name: 'Invisibility after attacking?',
    })
    await expect(selected).toHaveAttribute('aria-pressed', 'true')
    await expect(
      canvas.getByRole('button', { name: 'Shield spell: what does it stop?' }),
    ).toHaveAttribute('aria-pressed', 'false')
  },
}

/** The list is per-channel: switching the mode empties it, it does not merge. */
export const ChannelWithNoConversations: Story = {
  decorators: [withShell({ conversations: SAGE_THREAD, mode: 'rules' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Rules' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await expect(
      canvas.queryByRole('button', { name: /shield spell/ }),
    ).not.toBeInTheDocument()
  },
}

/** A player: the GM chip is not offered at all. */
export const Player: Story = {
  decorators: [withShell({ conversations: SAGE_THREAD, selected: 0, role: 'player' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  },
}

/**
 * Renaming, driven by keyboard alone: the rename button is reachable, the
 * input takes focus, and Enter commits.
 */
export const RenamedByKeyboard: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const rename = canvas.getByRole('button', {
      name: 'Rename Shield spell: what does it stop?',
    })
    rename.focus()
    await userEvent.keyboard('{Enter}')

    const input = canvas.getByRole('textbox', {
      name: 'Conversation title for Shield spell: what does it stop?',
    })
    await expect(input).toHaveFocus()
    await userEvent.keyboard('{Control>}a{/Control}Shield, settled')
    await userEvent.keyboard('{Enter}')

    await expect(canvas.getByRole('button', { name: 'Shield, settled' })).toBeInTheDocument()
  },
}

/** Escape abandons the rename and leaves the old title alone. */
export const RenameAbandoned: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const original = 'Shield spell: what does it stop?'
    canvas.getByRole('button', { name: `Rename ${original}` }).focus()
    await userEvent.keyboard('{Enter}')
    await userEvent.keyboard('{Control>}a{/Control}Something else entirely')
    await userEvent.keyboard('{Escape}')
    await expect(canvas.getByRole('button', { name: original })).toBeInTheDocument()
  },
}

/** Titles long enough to need truncation, in a list long enough to scroll. */
export const LongTitles: Story = {
  decorators: [
    withShell({
      conversations: Array.from({ length: 12 }, (_, i) => ({
        mode: 'sage' as const,
        firstPrompt: `Session ${i + 1}: the party argues about whether the door was trapped`,
      })),
      selected: 4,
    }),
  ],
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkNoConversations: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell()],
}
