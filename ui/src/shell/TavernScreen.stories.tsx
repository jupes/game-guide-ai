/**
 * TavernScreen -- the campaign screen at /tavern, in each list state, both
 * themes and on a phone (agent-forge-harness-74j; brief section 7.2, T-25a).
 *
 * The REAL campaign provider; only the network is stubbed. The harness's
 * `navIntent` is `undefined` here, so no story focuses the heading (that is
 * `TavernScreen.test.tsx`'s job, with a real `AppNavProvider`). Every story
 * passes axe (`test: 'error'`) and measures the 44 px floor of the controls
 * it shows.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from './campaignContext'
import { TavernScreen } from './TavernScreen'

const campaign = (id: string, name: string) => ({
  schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:31:24Z',
  archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e', avatar_icon: 'sailing',
  avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
})
const page = (items: unknown[]) => json({ schema_version: 1, items, next_cursor: null })

const withCampaigns: Decorator = (Story) => (
  <CampaignProvider>
    <Story />
  </CampaignProvider>
)
const withCampaignRestored: Decorator = (Story) => (
  <CampaignProvider restore={{ campaignId: 'cmp_1', conversationId: null }}>
    <Story />
  </CampaignProvider>
)

const meta = {
  title: 'Shell/TavernScreen',
  component: TavernScreen,
  parameters: { layout: 'fullscreen' },
  // Critic 47: the shell only. Each story names exactly one provider decorator, which renders inside this one (the provider needs the shell's CurrentUser).
  decorators: [withShell({ screen: 'tavern', mode: 'gm' })],
} satisfies Meta<typeof TavernScreen>

export default meta
type Story = StoryObj<typeof meta>
type Canvas = ReturnType<typeof within>
type Play = (canvas: Canvas) => Promise<void>

function state(route: (url: string, init: RequestInit | undefined) => Response | Promise<Response>, play: Play): Story {
  return {
    decorators: [withCampaigns],
    beforeEach: stubFetch(route),
    play: async ({ canvasElement }) => {
      const canvas = within(canvasElement)
      await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
      await play(canvas)
    },
  }
}
const dark = (story: Story): Story => ({
  ...story,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})

export const Loading = state(() => pending(), async (canvas) => {
  await expect(canvas.getByText('Loading campaigns…', { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, ['Back to chat'])
})
export const LoadingDark = dark(Loading)

export const FirstRun = state(() => page([]), async (canvas) => {
  await expect(await canvas.findByText('Create your first campaign', { exact: false })).toBeVisible()
  await expectTouchTargets(canvas, ['Create campaign', 'Back to chat'])
})
export const FirstRunDark = dark(FirstRun)

export const CouldNotLoad = state(() => json({}, 503), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(canvas.getByText("Couldn't load campaigns", { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', 'Back to chat'])
})
export const CouldNotLoadDark = dark(CouldNotLoad)

export const WithCampaigns: Story = {
  decorators: [withCampaignRestored],
  beforeEach: stubFetch((url) => (url === '/campaigns/cmp_1'
    ? json(campaign('cmp_1', 'The Drowned Crown'))
    : page([campaign('cmp_1', 'The Drowned Crown'), campaign('cmp_2', 'Ashes over Emberfall')]))),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
    await expect(await canvas.findByRole('button', { name: 'Continue without a campaign' })).toBeVisible()
    await expectTouchTargets(canvas, ['The Drowned Crown', 'Continue without a campaign', 'Back to chat'])
  },
}
export const WithCampaignsDark = dark(WithCampaigns)

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────

export const PhoneFirstRun: Story = {
  ...atViewport('phone390'),
  decorators: [withCampaigns],
  beforeEach: stubFetch(() => page([])),
  play: async ({ canvasElement }) => {
    await expectViewport('phone390')
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
    await expect(await canvas.findByText('Create your first campaign', { exact: false })).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Create campaign', 'Back to chat'])
  },
}

export const PhoneWithCampaignsDark: Story = {
  decorators: [withCampaignRestored],
  ...atViewport('phone390', 'dark'),
  beforeEach: stubFetch((url) => (url === '/campaigns/cmp_1'
    ? json(campaign('cmp_1', 'The Drowned Crown'))
    : page([campaign('cmp_1', 'The Drowned Crown')]))),
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expectViewport('phone390')
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
    await expect(await canvas.findByRole('button', { name: 'Continue without a campaign' })).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['The Drowned Crown', 'Continue without a campaign', 'Back to chat'])
  },
}
