/**
 * TavernSeatCard -- one of the caller's own seats, in each of its four
 * standings, in both themes and on a phone (agent-forge-harness-30c, PR-2).
 * Every story passes axe (`test: 'error'`); the phone stories also pass
 * `expectNoPageOverflow`. A seat card has nothing to press, so there are no
 * touch targets to measure: each story asserts there is no button or link.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import type { PlayerSeat } from '../gm/contracts'
import { TavernSeatCard } from './TavernSeatCard'

const base: PlayerSeat = {
  schema_version: 1, campaign_id: 'cmp_1', campaign_name: 'Gorath’s Table', alias: 'Brannoc',
  accepted_at: '2026-09-02T12:00:00Z', confirmed: true, tone: 'Mystery · Low magic', game_system: 'dnd5e',
  avatar_icon: 'castle', avatar_tone: 'gold', concluded: false, last_played_at: '2026-09-04T12:00:00Z', live: false,
}
const NOW = new Date('2026-10-01T09:00:00Z')

const meta = {
  title: 'Shell/TavernSeatCard',
  component: TavernSeatCard,
  parameters: { layout: 'padded' },
  args: { seat: base, now: NOW },
  decorators: [
    (Story) => (
      <div style={{ maxWidth: 360 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof TavernSeatCard>

export default meta
type Story = StoryObj<typeof meta>

const dark = (story: Story): Story => ({
  ...story,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})
const onPhone = (story: Story, theme: 'light' | 'dark' = 'light'): Story => ({
  ...story,
  ...atViewport('phone390', theme),
  play: async (context) => {
    await expectViewport('phone390')
    if (theme === 'dark') await expectTheme('dark')
    await story.play?.(context)
    await expectNoPageOverflow()
  },
})

async function expectNothingToPress(canvas: ReturnType<typeof within>): Promise<void> {
  await expect(canvas.queryAllByRole('button')).toHaveLength(0)
  await expect(canvas.queryAllByRole('link')).toHaveLength(0)
}

/** ID-17: a confirmed seat at a live table is plain text, never a link. */
export const Live: Story = {
  args: { seat: { ...base, live: true } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('group', { name: 'Gorath’s Table' })).toBeVisible()
    await expect(canvas.getByText('Live now')).toBeVisible()
    await expect(canvas.getByText('Playing as Brannoc')).toBeVisible()
    await expectNothingToPress(canvas)
  },
}
export const LiveDark = dark(Live)
export const PhoneLive = onPhone(Live)

/** D-12: waiting on the GM, even though the table is live. */
export const Waiting: Story = {
  args: { seat: { ...base, confirmed: false, live: true } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Waiting for your GM to confirm your seat')).toBeVisible()
    await expect(canvas.queryByText('Live now')).toBeNull()
    await expectNothingToPress(canvas)
  },
}
export const WaitingDark = dark(Waiting)
export const PhoneWaiting = onPhone(Waiting)

export const Quiet: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Last played 4 September')).toBeVisible()
    await expect(canvas.getByText('Mystery · Low magic')).toBeVisible()
    await expectNothingToPress(canvas)
  },
}
export const QuietDark = dark(Quiet)

export const Concluded: Story = {
  args: { seat: { ...base, concluded: true } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('This table has concluded')).toBeVisible()
    await expectNothingToPress(canvas)
  },
}
export const ConcludedDark = dark(Concluded)
export const PhoneConcluded = onPhone(Concluded, 'dark')
