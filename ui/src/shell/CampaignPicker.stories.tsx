/**
 * CampaignPicker -- §12.2's Campaign picker row, each state in both themes,
 * and on a phone (agent-forge-harness-1kg.2.5, PR-2; brief sections 9 T2-12
 * and 11; agent-forge-harness-0rn's phone rules).
 *
 * The picker runs against the REAL campaign provider; only the network is
 * stubbed. Every story passes axe (`test: 'error'`) and checks the 44 px floor
 * of the controls it shows at their call site.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from './campaignContext'
import { CampaignPicker } from './CampaignPicker'

const campaign = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:31:24Z',
  archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e', avatar_icon: 'sailing',
  avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false, ...over,
})
const CAMPAIGNS = [
  campaign('cmp_1', 'The Drowned Crown', { badge: 'live' }),
  campaign('cmp_2', 'Ashes over Emberfall, a very long campaign name that has to wrap on a phone', { badge: 'ready' }),
  campaign('cmp_3', 'Curse of the Hollow King', { concluded_at: '2026-09-20T00:00:00Z' }),
]
const page = (items: unknown[], next: string | null = null) => json({ schema_version: 1, items, next_cursor: next })

const withCampaigns: Decorator = (Story) => (
  <CampaignProvider>
    <div style={{ width: 'min(28rem, calc(100vw - 32px))' }}>
      <Story />
    </div>
  </CampaignProvider>
)

const meta = {
  title: 'Shell/CampaignPicker',
  component: CampaignPicker,
  // The provider needs the shell's CurrentUser: a later decorator wraps an earlier one.
  decorators: [withCampaigns, withShell({ mode: 'gm' })],
} satisfies Meta<typeof CampaignPicker>

export default meta
type Story = StoryObj<typeof meta>
type Play = NonNullable<Story['play']>

/** One state: its network, and what it shows. */
function state(
  route: (url: string, init: RequestInit | undefined) => Response | Promise<Response>,
  play: Play,
): Story {
  return { beforeEach: stubFetch(route), play }
}
const dark = (story: Story): Story => ({
  ...story,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})

const list = (items: unknown[], next: string | null = null) =>
  (_url: string, init: RequestInit | undefined) => (init?.method === 'POST' ? pending() : page(items, next))

async function typeName(canvasElement: HTMLElement, name: string): Promise<ReturnType<typeof within>> {
  const canvas = within(canvasElement)
  await userEvent.type(await canvas.findByLabelText('Campaign name'), name)
  return canvas
}

export const Loading = state(() => pending(), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await expect(await canvas.findByText('Loading campaigns…', { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, ['Create campaign'])
})
export const LoadingDark = dark(Loading)

export const Failed = state(() => json({}, 503), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await expect(await canvas.findByRole('button', { name: 'Retry' })).toBeVisible()
  // The visible line, not the status node; a failed read never invites a create.
  await expect(canvas.getByText("Couldn't load campaigns", { selector: 'p:not([role])' })).toBeVisible()
  await expect(canvas.queryByText('Create your first campaign — only a name is required')).toBeNull()
  await expectTouchTargets(canvas, ['Retry', 'Create campaign'])
})
export const FailedDark = dark(Failed)

export const Empty = state(list([]), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await expect(await canvas.findByText('Create your first campaign — only a name is required')).toBeVisible()
})
export const EmptyDark = dark(Empty)

export const Ready = state(list(CAMPAIGNS), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await expect(await canvas.findByRole('list', { name: 'Your campaigns' })).toBeVisible()
  await expectTouchTargets(canvas, ['The Drowned Crown LIVE', 'Curse of the Hollow King Concluded', 'Create campaign'])
})
export const ReadyDark = dark(Ready)

export const ReadyWithMore = state(list(CAMPAIGNS, 'next_1'), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await expect(await canvas.findByRole('button', { name: 'Load more' })).toBeVisible()
  await expectTouchTargets(canvas, ['Load more'])
})
export const ReadyWithMoreDark = dark(ReadyWithMore)

export const Creating = state(list([]), async ({ canvasElement }) => {
  const canvas = await typeName(canvasElement, 'The Sunken Library')
  await userEvent.click(canvas.getByRole('button', { name: 'Create campaign' }))
  await expect(canvas.getByRole('button', { name: 'Create campaign' })).toBeDisabled()
})
export const CreatingDark = dark(Creating)

export const CreateInvalid = state(list([]), async ({ canvasElement }) => {
  const canvas = within(canvasElement)
  await userEvent.click(await canvas.findByRole('button', { name: 'Create campaign' }))
  const field = canvas.getByLabelText('Campaign name')
  await expect(field).toHaveAttribute('aria-invalid', 'true')
  await expect(field).toHaveAccessibleDescription('Give the campaign a name of 1 to 120 characters on one line.')
})
export const CreateInvalidDark = dark(CreateInvalid)

export const CreateFailed = state(
  (_url, init) => (init?.method === 'POST' ? json({}, 503) : page([])),
  async ({ canvasElement }) => {
    const canvas = await typeName(canvasElement, 'The Sunken Library')
    await userEvent.click(canvas.getByRole('button', { name: 'Create campaign' }))
    await expect(await canvas.findByText("Couldn't create the campaign", { selector: 'p:not([role])' })).toBeVisible()
    await expect(canvas.getByLabelText('Campaign name')).toHaveFocus()
  },
)
export const CreateFailedDark = dark(CreateFailed)

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────
// Under 600px the form is one column; long names wrap; nothing scrolls the
// page sideways, and every control keeps the 44px floor.

async function phone(canvasElement: HTMLElement, viewport: 'phone320' | 'phone375'): Promise<void> {
  await expectViewport(viewport)
  const canvas = within(canvasElement)
  await expect(await canvas.findByRole('button', { name: 'Load more' })).toBeVisible()
  await expectNoPageOverflow()
  await expectTouchTargets(canvas, ['The Drowned Crown LIVE', 'Load more', 'Create campaign'])
  const field = canvas.getByLabelText('Campaign name').getBoundingClientRect()
  const create = canvas.getByRole('button', { name: 'Create campaign' }).getBoundingClientRect()
  await expect(create.top).toBeGreaterThanOrEqual(field.bottom)
}

export const Phone320: Story = {
  ...atViewport('phone320'),
  beforeEach: stubFetch(list(CAMPAIGNS, 'next_1')),
  play: async ({ canvasElement }) => phone(canvasElement, 'phone320'),
}

export const Phone375Dark: Story = {
  ...atViewport('phone375', 'dark'),
  beforeEach: stubFetch(list(CAMPAIGNS, 'next_1')),
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await phone(canvasElement, 'phone375')
  },
}
