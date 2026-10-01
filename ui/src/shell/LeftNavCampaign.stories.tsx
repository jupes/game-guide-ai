/**
 * LeftNav in a campaign's GM channel -- the campaign line and the campaign's
 * GM threads in each state, in both themes and on a phone
 * (agent-forge-harness-1kg.2.5, PR-2b; brief sections 7.7 and 11, the
 * Critic's item 5; agent-forge-harness-0rn's phone rules).
 *
 * The REAL campaign provider restores `cmp_1` as a cold load would; only the
 * network is stubbed. The harness sits on Landing so the provider writes no
 * fragment into the preview's URL (it writes one only in the workspace).
 * Every story passes axe (`test: 'error'`) and measures the 44 px floor of the
 * controls it shows.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from './campaignContext'
import { LeftNav } from './LeftNav'

const LONG_NAME = 'Ashes over Emberfall, a very long campaign name that has to wrap on a phone'
const campaign = (name: string) => ({
  schema_version: 1, campaign_id: 'cmp_1', name, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:31:24Z',
  archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e', avatar_icon: 'sailing',
  avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
})
const thread = (id: string, title: string | null) => ({
  schema_version: 1, conversation_id: id, campaign_id: 'cmp_1', title, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null,
})
const THREADS = [thread('cnv_1', 'The heist at Saltmarsh'), thread('cnv_2', null)]

type Answer = () => Response | Promise<Response>
interface Network { name?: string; read?: Answer; list?: Answer; next?: string }
/** The campaign read and the thread list answered as a state needs; a rename always fails. */
const network = ({ name = 'The Drowned Crown', read, list, next }: Network = {}) => stubFetch((url, init) => {
  if (init?.method === 'PATCH') return json({}, 503)
  if (url === '/campaigns/cmp_1') return read?.() ?? json(campaign(name))
  if (url.startsWith('/conversations?campaign_id=cmp_1')) return list?.() ?? json({ schema_version: 1, items: THREADS, next_cursor: next ?? null })
  return json({}, 404)
})

const withCampaign: Decorator = (Story) => (
  <CampaignProvider restore={{ campaignId: 'cmp_1', conversationId: null }}>
    <Story />
  </CampaignProvider>
)

const meta = {
  title: 'Shell/LeftNav/Campaign',
  component: LeftNav,
  parameters: { layout: 'fullscreen' },
  // The provider needs the shell's CurrentUser: a later decorator wraps an earlier one.
  decorators: [withCampaign, withShell({ screen: 'landing', mode: 'gm' })],
} satisfies Meta<typeof LeftNav>

export default meta
type Story = StoryObj<typeof meta>
type Canvas = ReturnType<typeof within>

const state = (net: Network, play: (canvas: Canvas) => Promise<void>): Story => ({
  beforeEach: network(net),
  play: ({ canvasElement }) => play(within(canvasElement)),
})
const dark = (story: Story): Story => ({
  ...story,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})

export const Selected = state({}, async (canvas) => {
  await expect(await canvas.findByText('Campaign: The Drowned Crown')).toBeVisible()
  await expect(await canvas.findByRole('button', { name: 'The heist at Saltmarsh' })).toHaveAttribute('aria-pressed', 'false')
  await expect(canvas.getByRole('button', { name: /^GM thread · / })).toBeVisible()
  await expectTouchTargets(canvas, ['New conversation', 'The heist at Saltmarsh', 'Rename The heist at Saltmarsh'])
})
export const SelectedDark = dark(Selected)

export const Restoring = state({ read: pending }, async (canvas) => {
  await expect(await canvas.findByText('Loading campaign…')).toBeVisible()
  await expect(canvas.queryByText('Conversations')).toBeNull()
})
export const RestoringDark = dark(Restoring)

export const CampaignFailed = state({ read: () => json({}, 503) }, async (canvas) => {
  await expect(await canvas.findByText("Couldn't load campaigns")).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', 'Continue without a campaign'])
})
export const CampaignFailedDark = dark(CampaignFailed)

export const Unavailable = state({ read: () => json({}, 404) }, async (canvas) => {
  await expect(await canvas.findByText("That campaign isn't available.")).toBeVisible()
  await expect(canvas.queryByRole('button', { name: 'Retry' })).toBeNull()
  await expectTouchTargets(canvas, ['Continue without a campaign'])
})
export const UnavailableDark = dark(Unavailable)

export const ThreadsLoading = state({ list: pending }, async (canvas) => {
  await expect(await canvas.findByText('Loading conversations…')).toBeVisible()
  const skeletons = document.querySelectorAll('.left-nav__skeleton')
  await expect(skeletons).toHaveLength(2)
  for (const row of skeletons) await expect(row).toHaveAttribute('aria-hidden', 'true')
})
export const ThreadsLoadingDark = dark(ThreadsLoading)

export const ThreadsFailed = state({ list: () => json({}, 503) }, async (canvas) => {
  await expect(await canvas.findByText("Couldn't load conversations")).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', 'New conversation'])
})
export const ThreadsFailedDark = dark(ThreadsFailed)

export const WithMore = state({ next: 'more_1' }, async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Load more' })).toBeVisible()
  await expectTouchTargets(canvas, ['Load more'])
})
export const WithMoreDark = dark(WithMore)

/** A rename the server could not take: the text stays, the error is described, and Retry is offered. */
export const RenameFailed = state({}, async (canvas) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'Rename The heist at Saltmarsh' }))
  const input = canvas.getByRole('textbox', { name: 'Conversation title for The heist at Saltmarsh' })
  await userEvent.type(input, ', part two{Enter}')
  await expect(await canvas.findByText("Couldn't rename the conversation.")).toBeVisible()
  await expect(input).toHaveValue('The heist at Saltmarsh, part two')
  await expect(input).toHaveAccessibleDescription("Couldn't rename the conversation.")
  await expectTouchTargets(canvas, ['Retry'])
})
export const RenameFailedDark = dark(RenameFailed)

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────
// The line wraps a long name and its buttons; nothing scrolls the page
// sideways, and every control keeps the 44 px floor.

export const Phone320: Story = {
  ...atViewport('phone320'),
  ...state({ name: LONG_NAME }, async (canvas) => {
    await expectViewport('phone320')
    await expect(await canvas.findByText(`Campaign: ${LONG_NAME}`)).toBeVisible()
    await canvas.findByRole('button', { name: 'The heist at Saltmarsh' })
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['New conversation', 'The heist at Saltmarsh'])
  }),
}

export const Phone375FailedDark: Story = {
  ...atViewport('phone375', 'dark'),
  ...state({ read: () => json({}, 503) }, async (canvas) => {
    await expectTheme('dark')
    await expectViewport('phone375')
    await expect(await canvas.findByText("Couldn't load campaigns")).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Retry', 'Continue without a campaign'])
  }),
}
