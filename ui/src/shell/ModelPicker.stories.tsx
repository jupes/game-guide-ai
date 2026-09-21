/**
 * ModelPicker — the per-conversation model preference (b8o.2).
 *
 * The rule it enforces is "conversation affinity": before the first prompt the
 * preference is free to change, and after it a change starts a NEW conversation
 * rather than quietly diverging from what the server already committed to. Both
 * halves have stories, because the difference is invisible in a screenshot.
 */
import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { ModelPicker } from './ModelPicker'
import type { ModelPickerProps } from './ModelPicker'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'

const CATALOG = {
  default: 'auto',
  models: [
    { id: 'auto', display_name: 'Automatic' },
    { id: 'haiku', display_name: 'Haiku — fast', tier: 'cheap' },
    { id: 'sonnet', display_name: 'Sonnet — balanced', tier: 'default' },
    { id: 'opus', display_name: 'Opus — deepest', tier: 'top' },
  ],
}

/**
 * A visible readout of what the store actually did. "Started a new
 * conversation" and "changed this one" look identical on screen otherwise.
 */
function StoreReadout(): React.JSX.Element {
  const store = useConversationStore()
  const { mode, conversationId } = useAppNav()
  const rows = store.list(mode)
  const openIndex = rows.findIndex((row) => row.id === conversationId)
  return (
    <p>
      {rows.length} conversation{rows.length === 1 ? '' : 's'} · open #{openIndex + 1} · preference{' '}
      {rows[openIndex]?.modelPreference ?? '—'}
    </p>
  )
}

function WithReadout(props: ModelPickerProps): React.JSX.Element {
  return (
    <div>
      <ModelPicker {...props} />
      <StoreReadout />
    </div>
  )
}

const meta = {
  title: 'Shell/ModelPicker',
  component: ModelPicker,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: { getModels: async () => CATALOG },
  decorators: [
    withShell({ conversations: [{ mode: 'sage', modelPreference: 'auto' }], selected: 0 }),
  ],
} satisfies Meta<typeof ModelPicker>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/**
 * Nothing open, so there is nothing to bind a preference to. Disabled, not
 * hidden — the control stays where the eye expects it.
 */
export const NoConversationOpen: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('combobox', { name: 'Model' })).toBeDisabled()
  },
}

/**
 * The catalog request failed. The fallback is a one-entry catalog, so the
 * control degrades to "Automatic" instead of rendering an empty menu.
 */
export const CatalogUnavailable: Story = {
  args: { getModels: async () => Promise.reject(new Error('502')) },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const select = canvas.getByRole('combobox', { name: 'Model' })
    await expect(within(select).getAllByRole('option')).toHaveLength(1)
    await expect(select).toHaveValue('auto')
  },
}

/**
 * Before the first prompt: keyboard-reachable, and the change binds to THIS
 * conversation. No confirmation, because nothing has been committed yet.
 *
 * Tab is a real key press. The value change goes through `selectOptions`: a
 * native `<select>`'s option list is chrome the page cannot see, and opening it
 * is a browser default action that only runs for trusted events — so arrowing
 * through it is not something this runner can synthesise. What Tab proves is
 * the part that could actually be got wrong: that the control is in the tab
 * order at all.
 */
export const ChangedBeforeFirstPrompt: Story = {
  render: (args) => <WithReadout {...args} />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/1 conversation · open #1 · preference auto/)).toBeInTheDocument()

    await userEvent.tab()
    const select = canvas.getByRole('combobox', { name: 'Model' })
    await expect(select).toHaveFocus()

    await userEvent.selectOptions(select, 'opus')
    await expect(select).toHaveValue('opus')
    await expect(canvas.getByText(/1 conversation · open #1 · preference opus/)).toBeInTheDocument()
  },
}

/**
 * After the first prompt: the change is confirmed, and it opens a SECOND
 * conversation rather than rewriting the strategy the server already bound.
 */
export const ChangedAfterFirstPromptStartsANewConversation: Story = {
  args: { confirmChange: fn(() => true) },
  decorators: [
    withShell({
      conversations: [{ mode: 'sage', firstPrompt: 'How does grappling work?', modelPreference: 'auto' }],
      selected: 0,
    }),
  ],
  render: (args) => <WithReadout {...args} />,
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const select = canvas.getByRole('combobox', { name: 'Model' })
    await userEvent.selectOptions(select, 'sonnet')

    await expect(args.confirmChange).toHaveBeenCalledWith(
      'Start a new conversation with Sonnet — balanced?',
    )
    await expect(
      canvas.getByText(/2 conversations · open #2 · preference sonnet/),
    ).toBeInTheDocument()
  },
}

/** The same change, declined: nothing is created and nothing is rebound. */
export const ChangeDeclined: Story = {
  args: { confirmChange: () => false },
  decorators: [
    withShell({
      conversations: [{ mode: 'sage', firstPrompt: 'How does grappling work?', modelPreference: 'auto' }],
      selected: 0,
    }),
  ],
  render: (args) => <WithReadout {...args} />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.selectOptions(canvas.getByRole('combobox', { name: 'Model' }), 'haiku')
    await expect(canvas.getByText(/1 conversation · open #1 · preference auto/)).toBeInTheDocument()
  },
}

/** A catalog with names long enough to stretch the control. */
export const LongModelNames: Story = {
  args: {
    getModels: async () => ({
      default: 'auto',
      models: [
        { id: 'auto', display_name: 'Automatic — let the router decide per question' },
        { id: 'long', display_name: 'Extremely Verbose Model Name, Preview Edition (2026-09-01)' },
      ],
    }),
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkDisabled: Story = {
  globals: { theme: 'dark' },
  decorators: [withShell()],
}
