/**
 * TopBar — brand, plus the open conversation's title when there is one.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { withShell } from '../../.storybook/shellHarness'
import { TopBar } from './TopBar'

const meta = {
  title: 'Shell/TopBar',
  component: TopBar,
  tags: ['autodocs'],
  parameters: { layout: 'fullscreen' },
} satisfies Meta<typeof TopBar>

export default meta
type Story = StoryObj<typeof meta>

/** Nothing open: brand only, and no empty title slot left behind. */
export const NoConversationOpen: Story = {
  decorators: [withShell()],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Aetheril')).toBeInTheDocument()
    await expect(canvasElement.querySelector('.top-bar__conversation-title')).toBeNull()
  },
}

export const WithOpenConversation: Story = {
  decorators: [
    withShell({
      conversations: [{ mode: 'sage', firstPrompt: 'What does a shield spell actually stop?' }],
      selected: 0,
    }),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    // The title is derived from the first prompt, capped at 40 code points.
    await expect(canvas.getByText('What does a shield spell actually stop?')).toBeInTheDocument()
  },
}

/**
 * A renamed conversation with no natural break in it. The title is announced
 * politely, so it must not be allowed to push the brand off the bar.
 */
export const LongTitle: Story = {
  decorators: [
    withShell({
      conversations: [
        {
          mode: 'gm',
          firstPrompt: 'plan',
          title:
            'Session 14 — the long walk back through the Underdark after the bridge collapsed and nobody wanted to talk about whose fault that was',
        },
      ],
      selected: 0,
      mode: 'gm',
    }),
  ],
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  decorators: [
    withShell({
      conversations: [{ mode: 'rules', firstPrompt: 'Can you ready a spell and then move?' }],
      selected: 0,
      mode: 'rules',
    }),
  ],
}
