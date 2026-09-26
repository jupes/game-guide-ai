/**
 * AppNav — the shell's navigation state machine.
 *
 * There is no UI to photograph here, so these stories document the transitions
 * instead, driven through the REAL `AppNavProvider` (not the story harness) and
 * read back from `useAppNav`. Two of them are contracts that are easy to break
 * and invisible on screen:
 *
 *  - `enterWorkspace()` RESETS the mode to sage; `backToWorkspace()` leaves it
 *    alone, so a DM who opens Profile from the GM channel returns to the GM
 *    channel.
 *  - `conversationId` is navigation state, not conversation data: it survives a
 *    mode change, and the screens are free to clear it.
 */
import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { AppNavProvider, useAppNav } from './AppNav'
import type { ChatMode } from './AppNav'

const MODES: ChatMode[] = ['sage', 'spell', 'rules', 'gm']

/** A readout plus one control per transition — the state machine, made visible. */
function NavigationPanel(): React.JSX.Element {
  const nav = useAppNav()
  return (
    <div style={{ display: 'grid', gap: 12, padding: 16, fontFamily: 'system-ui' }}>
      <p>
        screen: <strong>{nav.screen}</strong> · mode: <strong>{nav.mode}</strong> · conversation:{' '}
        <strong>{nav.conversationId ?? 'none'}</strong>
      </p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <button type="button" onClick={() => nav.enterWorkspace()}>
          enterWorkspace()
        </button>
        <button type="button" onClick={() => nav.backToLanding()}>
          backToLanding()
        </button>
        <button type="button" onClick={() => nav.openProfile()}>
          openProfile()
        </button>
        <button type="button" onClick={() => nav.backToWorkspace()}>
          backToWorkspace()
        </button>
        <button type="button" onClick={() => nav.setConversationId('conv-7')}>
          setConversationId(&quot;conv-7&quot;)
        </button>
        <button type="button" onClick={() => nav.setConversationId(null)}>
          setConversationId(null)
        </button>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {MODES.map((mode) => (
          <button key={mode} type="button" onClick={() => nav.setMode(mode)}>
            setMode(&quot;{mode}&quot;)
          </button>
        ))}
        {MODES.map((mode) => (
          <button key={`enter-${mode}`} type="button" onClick={() => nav.enterWorkspace(mode)}>
            enterWorkspace(&quot;{mode}&quot;)
          </button>
        ))}
      </div>
    </div>
  )
}

const meta = {
  title: 'Shell/AppNav',
  component: NavigationPanel,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
  decorators: [
    (Story) => (
      <AppNavProvider>
        <Story />
      </AppNavProvider>
    ),
  ],
} satisfies Meta<typeof NavigationPanel>

export default meta
type Story = StoryObj<typeof meta>

/** The starting point: landing, sage, nothing open. */
export const InitialState: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/screen:/)).toHaveTextContent(
      'screen: landing · mode: sage · conversation: none',
    )
  },
}

/** A channel shortcut carries its mode into the workspace. */
export const EnterOnAChannel: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'enterWorkspace("gm")' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('screen: workspace · mode: gm')
  },
}

/**
 * The distinction the whole context turns on. `openProfile` then
 * `backToWorkspace` preserves the channel; `enterWorkspace()` with no argument
 * resets it to sage.
 */
export const ProfileRoundTripKeepsTheChannel: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const press = async (name: string) => {
      canvas.getByRole('button', { name }).focus()
      await userEvent.keyboard('{Enter}')
    }

    await press('enterWorkspace("gm")')
    await press('openProfile()')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('screen: profile · mode: gm')

    await press('backToWorkspace()')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('screen: workspace · mode: gm')

    // …and the no-argument entry point is the one that resets it.
    await press('enterWorkspace()')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('screen: workspace · mode: sage')
  },
}

/** The open conversation is navigation state: a channel change does not clear it. */
export const ConversationSurvivesAModeChange: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const press = async (name: string) => {
      canvas.getByRole('button', { name }).focus()
      await userEvent.keyboard('{Enter}')
    }

    await press('setConversationId("conv-7")')
    await press('setMode("rules")')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent(
      'mode: rules · conversation: conv-7',
    )

    await press('setConversationId(null)')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('conversation: none')
  },
}

/** Back to landing from anywhere, without disturbing the channel. */
export const BackToLanding: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const press = async (name: string) => {
      canvas.getByRole('button', { name }).focus()
      await userEvent.keyboard('{Enter}')
    }
    await press('enterWorkspace("spell")')
    await press('backToLanding()')
    await expect(canvas.getByText(/screen:/)).toHaveTextContent('screen: landing · mode: spell')
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}
