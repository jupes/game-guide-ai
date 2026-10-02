/**
 * TavernCampaignCard -- one owned campaign in its three variants, with each
 * badge and with none, in both themes and on a phone (agent-forge-harness-30c,
 * PR-1). Every story passes axe (`test: 'error'`) and measures the 44 px floor
 * of the actions it shows; the phone stories also pass `expectNoPageOverflow`.
 */
import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import type { Campaign } from '../gm/contracts'
import { TavernCampaignCard } from './TavernCampaignCard'

const base: Campaign = {
  schema_version: 1, campaign_id: 'cmp_1', name: 'The Hollow Crown', created_at: '2026-09-01T12:00:00Z',
  updated_at: '2026-09-01T12:00:00Z', archived_at: null, concluded_at: null, tone: 'Grim and gothic',
  game_system: 'dnd5e', avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 4,
  last_activity_at: '2026-09-20T12:00:00Z', last_played_at: null, dormant: false,
}
const NOW = new Date('2026-10-01T09:00:00Z')

const meta = {
  title: 'Shell/TavernCampaignCard',
  component: TavernCampaignCard,
  parameters: { layout: 'padded' },
  args: {
    campaign: base,
    variant: 'active',
    busy: false,
    now: NOW,
    landing: React.createRef<HTMLElement>(),
    onPrep: () => {},
    onConclude: () => {},
    onReopen: () => {},
  },
  decorators: [
    (Story) => (
      <div style={{ maxWidth: 360 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof TavernCampaignCard>

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

export const Active: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('group', { name: 'The Hollow Crown' })).toBeVisible()
    await expect(canvas.getByText('Grim and gothic')).toBeVisible()
    await expect(canvas.getByText('4 players')).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Start Session The Hollow Crown' })).toHaveAttribute('aria-disabled', 'true')
    await expectTouchTargets(canvas, ['Prep The Hollow Crown', 'Start Session The Hollow Crown'])
  },
}
export const ActiveDark = dark(Active)
export const PhoneActive = onPhone(Active)
export const PhoneActiveDark = onPhone(Active, 'dark')

export const NoBadgeNoTone: Story = {
  args: { campaign: { ...base, tone: null, seat_count: 0 } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByText('LIVE')).toBeNull()
    await expect(canvas.queryByText('READY')).toBeNull()
    await expect(canvas.queryByText(/player/)).toBeNull()
    await expectTouchTargets(canvas, ['Prep The Hollow Crown'])
  },
}

export const Live: Story = {
  args: { campaign: { ...base, badge: 'live' } },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('LIVE')).toBeVisible()
  },
}
export const LiveDark = dark(Live)

export const Ready: Story = {
  args: { campaign: { ...base, badge: 'ready' } },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('READY')).toBeVisible()
  },
}
export const ReadyDark = dark(Ready)

export const Dormant: Story = {
  args: { variant: 'dormant', campaign: { ...base, dormant: true, last_activity_at: '2026-06-04T12:00:00Z' } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Dormant · no activity since 4 June')).toBeVisible()
    await expect(canvas.queryByRole('button', { name: /^Start Session/ })).toBeNull()
    await expectTouchTargets(canvas, ['Prep The Hollow Crown', 'Mark concluded The Hollow Crown'])
  },
}
export const DormantDark = dark(Dormant)
export const PhoneDormant = onPhone(Dormant)
export const PhoneDormantDark = onPhone(Dormant, 'dark')

export const Concluded: Story = {
  args: { variant: 'concluded', campaign: { ...base, concluded_at: '2026-09-25T12:00:00Z' } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: /^Start Session/ })).toBeNull()
    await expectTouchTargets(canvas, ['Prep The Hollow Crown', 'Reopen The Hollow Crown'])
  },
}
export const ConcludedDark = dark(Concluded)
export const PhoneConcluded = onPhone(Concluded)

export const LongName: Story = {
  args: {
    campaign: { ...base, name: 'The Interminable and Exceedingly Long-Named Chronicle of the Seven Drowned Crowns of Emberfall' },
  },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('heading', { level: 2 })).toBeVisible()
  },
}
export const PhoneLongName = onPhone(LongName)
