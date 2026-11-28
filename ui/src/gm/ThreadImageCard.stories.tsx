import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'
import { ThreadImageCard } from './ThreadImageCard'

const unavailable = fn(async () => new Response(null, { status: 404 }))

const meta = {
  title: 'Aetheril/Thread image',
  component: ThreadImageCard,
  args: {
    campaignId: 'cmp_harbour', assetId: 'ast_map', use: 'map', entryId: 'ent_map',
    scope: { actorId: 'gm', conversationId: 'cnv_harbour', onSaved: fn() },
    fetchImpl: unavailable,
  },
} satisfies Meta<typeof ThreadImageCard>
export default meta
type Story = StoryObj<typeof meta>

export const UnavailableAttachment: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const retry = await canvas.findByRole('button', { name: 'Retry image' })
    await userEvent.click(retry)
    await expect(await canvas.findByText('This image is unavailable.')).toBeVisible()
    await expect(canvas.queryByRole('img')).toBeNull()
    await expect(canvas.queryByRole('button', { name: 'Save as handout' })).toBeNull()
  },
}
