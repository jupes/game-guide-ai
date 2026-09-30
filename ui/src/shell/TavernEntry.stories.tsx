/**
 * LeftNav's tavern entry -- Choose a campaign / Switch campaign, in both
 * themes and on a phone (agent-forge-harness-74j; brief section C.8, T-25b).
 *
 * The REAL campaign provider; only the network is stubbed. The harness sits
 * on Landing so the provider writes no fragment into the preview's URL.
 * Every story passes axe (`test: 'error'`) and measures the 44 px floor.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from './campaignContext'
import { LeftNav } from './LeftNav'

const campaign = (id: string, name: string) => ({
  schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:31:24Z',
  archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e', avatar_icon: 'sailing',
  avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
})
const thread = (id: string, campaignId: string) => ({
  schema_version: 1, conversation_id: id, campaign_id: campaignId, title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
})

const withCampaign: Decorator = (Story) => (
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
  title: 'Shell/LeftNav/TavernEntry',
  component: LeftNav,
  parameters: { layout: 'fullscreen' },
  // The provider needs the shell's CurrentUser: a later decorator wraps an earlier one.
  decorators: [withCampaign, withShell({ screen: 'landing', mode: 'gm' })],
} satisfies Meta<typeof LeftNav>

export default meta
type Story = StoryObj<typeof meta>

export const ChooseNone: Story = {
  beforeEach: stubFetch(() => json({}, 404)),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('button', { name: 'Choose a campaign' })).toBeVisible()
    await expectTouchTargets(canvas, ['Choose a campaign'])
  },
}
export const ChooseNoneDark: Story = {
  ...ChooseNone,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await ChooseNone.play?.(context)
  },
}

export const SwitchSelected: Story = {
  decorators: [withCampaignRestored, withShell({ screen: 'landing', mode: 'gm' })],
  beforeEach: stubFetch((url) => (url === '/campaigns/cmp_1'
    ? json(campaign('cmp_1', 'The Drowned Crown'))
    : url.startsWith('/conversations?campaign_id=cmp_1')
      ? json({ schema_version: 1, items: [thread('cnv_1', 'cmp_1')], next_cursor: null })
      : json({}, 404))),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText('Campaign: The Drowned Crown')).toBeVisible()
    await expect(await canvas.findByRole('button', { name: 'Switch campaign' })).toBeVisible()
    await expect(canvas.queryByRole('button', { name: 'Choose a campaign' })).toBeNull()
    await expectTouchTargets(canvas, ['Switch campaign'])
  },
}
export const SwitchSelectedDark: Story = {
  ...SwitchSelected,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await SwitchSelected.play?.(context)
  },
}

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────

export const Phone320ChooseNone: Story = {
  ...atViewport('phone320'),
  beforeEach: stubFetch(() => json({}, 404)),
  play: async ({ canvasElement }) => {
    await expectViewport('phone320')
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('button', { name: 'Choose a campaign' })).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Choose a campaign'])
  },
}
