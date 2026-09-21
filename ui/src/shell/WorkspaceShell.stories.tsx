/**
 * WorkspaceShell — the whole signed-in workspace: TopBar, AppHeader, LeftNav
 * and ChatPane assembled.
 *
 * Nothing inside takes props, so the seam is the network. One stub answers
 * every endpoint the workspace reaches for on mount (`/models`, a
 * conversation's messages, its attachments) and `/chat` when a turn is sent.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { WorkspaceShell } from './WorkspaceShell'

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'sonnet', display_name: 'Sonnet — balanced' },
  ],
}

const MESSAGES = [
  {
    id: 1,
    role: 'user',
    content: 'What does a shield spell stop?',
    mode: 'sage',
    created_at: '2026-09-18T19:02:00Z',
  },
  {
    id: 2,
    role: 'assistant',
    content: 'It stops the triggering attack, and *magic missile* outright.',
    mode: 'sage',
    created_at: '2026-09-18T19:02:04Z',
  },
]

/**
 * Answers everything the workspace asks for on mount, plus a chat turn.
 * `conversation_id` is not optional decoration: the client validates both
 * bodies with zod, and a response without it degrades to "unreadable".
 */
function workspaceApi(messages: unknown[] = MESSAGES) {
  return stubFetch((url) => {
    if (url.includes('/models')) return json(CATALOG)
    if (url.includes('/messages')) return json({ conversation_id: 'story', messages })
    if (url.includes('/attachments')) return json({ conversation_id: 'story', attachments: [] })
    if (url.includes('/chat')) {
      return json({ answer: 'A reaction, and worth the slot.', sources: [], answerable: true })
    }
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

const meta = {
  title: 'Shell/WorkspaceShell',
  component: WorkspaceShell,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  beforeEach: workspaceApi(),
  decorators: [
    withShell({
      conversations: [
        { mode: 'sage', firstPrompt: 'Shield spell: what does it stop?' },
        { mode: 'sage', firstPrompt: 'Grappling, briefly' },
      ],
      selected: 0,
    }),
  ],
} satisfies Meta<typeof WorkspaceShell>

export default meta
type Story = StoryObj<typeof meta>

/** The workspace as a DM opens it: a conversation selected and recalled. */
export const Playground: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText(/magic missile/)).toBeInTheDocument()
    // Brand lives once, in the TopBar (swe1.10) — never duplicated in the nav.
    await expect(canvas.getAllByText('Aetheril')).toHaveLength(1)
  },
}

/** A fresh account: no conversations, and the channel's own empty prompt. */
export const NothingYet: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('Ask the Sage…')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'New conversation' })).toBeEnabled()
  },
}

/** History recall failed for the open conversation. The workspace still works. */
export const HistoryUnavailable: Story = {
  beforeEach: stubFetch((url) => {
    if (url.includes('/models')) return json(CATALOG)
    if (url.includes('/messages')) return json({ detail: 'nope' }, 503)
    if (url.includes('/attachments')) return json({ conversation_id: 'story', attachments: [] })
    return json({ detail: 'unrouted' }, 404)
  }),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('textbox')).toBeEnabled()
  },
}

/** A player: three channels, and no GM chip anywhere in the shell. */
export const Player: Story = {
  decorators: [
    withShell({ role: 'player', conversations: [{ mode: 'sage' }], selected: 0 }),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'GM' })).not.toBeInTheDocument()
  },
}

/**
 * Keyboard only, across the whole shell: open the second conversation from the
 * nav with Enter and watch the TopBar title follow.
 */
export const ConversationOpenedByKeyboard: Story = {
  beforeEach: workspaceApi([]),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const second = await canvas.findByRole('button', { name: 'Grappling, briefly' })
    second.focus()
    await userEvent.keyboard('{Enter}')
    await expect(second).toHaveAttribute('aria-pressed', 'true')
    await expect(
      canvasElement.querySelector('.top-bar__conversation-title'),
    ).toHaveTextContent('Grappling, briefly')
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkNothingYet: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell()],
}
