/**
 * TavernScreen -- "Your Campaigns" at /tavern, in each state of the spec's
 * section 4, both themes, and on a phone (agent-forge-harness-74j routed it;
 * 30c PR-1 builds it).
 *
 * The REAL campaign provider; only the network is stubbed. The harness's
 * `navIntent` is `undefined` here, so no story focuses the heading or a card
 * (that is `TavernScreen.test.tsx`'s job, with a real `AppNavProvider`). Every
 * story passes axe (`test: 'error'`) and measures the 44 px floor of the
 * controls it shows; the phone stories also pass `expectNoPageOverflow`.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from './campaignContext'
import { TavernScreen } from './TavernScreen'

const campaign = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, campaign_id: id, name, created_at: '2026-09-16T19:20:11Z', updated_at: '2026-09-16T19:31:24Z',
  archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e', avatar_icon: 'sailing',
  avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false, ...over,
})
const page = (items: unknown[]) => json({ schema_version: 1, items, next_cursor: null })

/** Activity on day `n` of September 2026 (the later, the more recent). */
const day = (n: number): string => `2026-09-${String(n).padStart(2, '0')}T12:00:00Z`

const SEVERAL = [
  campaign('cmp_1', 'The Drowned Crown', { last_activity_at: day(25), badge: 'live', seat_count: 4, tone: 'Grim and gothic' }),
  campaign('cmp_2', 'Ashes over Emberfall', { last_activity_at: day(24), badge: 'ready', seat_count: 1, avatar_tone: 'gold' }),
  campaign('cmp_3', 'The Hollow Crown', { last_activity_at: day(20) }),
  campaign('cmp_4', 'Salt and Starlight', { last_activity_at: '2026-06-04T12:00:00Z', dormant: true, tone: 'Quiet seafaring mystery' }),
  campaign('cmp_5', 'Barrow of the Wren Queen', { concluded_at: day(10), last_activity_at: day(9) }),
  campaign('cmp_6', 'A Ledger of Small Mercies', { concluded_at: day(8), last_activity_at: day(7) }),
]
const THIRTEEN = Array.from({ length: 13 }, (_, i) => (
  campaign(`cmp_${i + 1}`, `Campaign ${String(i + 1).padStart(2, '0')}`, { last_activity_at: day(28 - i) })
))

const withCampaigns: Decorator = (Story) => (
  <CampaignProvider>
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

type Route = (url: string, init: RequestInit | undefined) => Response | Promise<Response>
/** The campaign states' network: every `GET /seats` answers `seats` (none unless a story seats the GM). */
const withSeats = (route: Route, seats: () => Response | Promise<Response> = () => page([])): Route => (url, init) => (
  url.startsWith('/seats') ? seats() : route(url, init)
)

function state(route: Route, play: Play): Story {
  return {
    decorators: [withCampaigns],
    beforeEach: stubFetch(withSeats(route)),
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

const HEADER = ['New Campaign', 'Back to chat']

export const Loading = state(() => pending(), async (canvas) => {
  await expect(canvas.getByText('Loading campaigns…', { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, [...HEADER, 'Create campaign'])
})
export const LoadingDark = dark(Loading)

/** U-10 / ID-16: a brand-new GM gets the empty line and Begin anew. */
export const BrandNew = state(() => page([]), async (canvas) => {
  await expect(await canvas.findByText('Create your first campaign', { exact: false })).toBeVisible()
  await expect(canvas.getByRole('heading', { name: 'Begin anew', level: 2 })).toBeVisible()
  await expectTouchTargets(canvas, [...HEADER, 'Create campaign'])
})
export const BrandNewDark = dark(BrandNew)

export const CouldNotLoad = state(() => json({}, 503), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(canvas.getByText("Couldn't load campaigns", { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', ...HEADER, 'Create campaign'])
})
export const CouldNotLoadDark = dark(CouldNotLoad)

/** E-2 / ID-15: "Where you were", seeded through the shell's conversation store. */
export const WhereYouWere: Story = {
  decorators: [
    withCampaigns,
    withShell({ screen: 'tavern', mode: 'gm', conversations: [{ mode: 'sage', firstPrompt: 'What does a fireball do?' }] }),
  ],
  beforeEach: stubFetch(() => page([])),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('heading', { name: 'Where you were', level: 2 })).toBeVisible()
    await expect(canvas.getByText('Sage · “What does a fireball do?”')).toBeVisible()
    await expect(canvas.getByText('Create your first campaign', { exact: false })).toBeVisible()
    await expectTouchTargets(canvas, ['Back to that thread', ...HEADER, 'Create campaign'])
  },
}
export const WhereYouWereDark = dark(WhereYouWere)

/** E-3: exactly one active campaign. */
export const OneCampaign = state(() => page([SEVERAL[0]]), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Prep The Drowned Crown' })).toBeVisible()
  await expect(canvas.getByRole('button', { name: 'Start Session The Drowned Crown' })).toHaveAttribute('aria-disabled', 'true')
  await expectTouchTargets(canvas, ['Prep The Drowned Crown', 'Start Session The Drowned Crown', ...HEADER, 'Create campaign'])
})
export const OneCampaignDark = dark(OneCampaign)
export const PhoneOneCampaign = onPhone(OneCampaign)

/** E-5 with E-4: several campaigns, a LIVE and a READY badge, a dormant card, and Concluded (2). */
export const Several = state(() => page(SEVERAL), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Prep Ashes over Emberfall' })).toBeVisible()
  await expect(canvas.getByText('LIVE')).toBeVisible()
  await expect(canvas.getByText('READY')).toBeVisible()
  await expect(canvas.getByText(/^Dormant · no activity since /)).toBeVisible()
  await expect(canvas.getByRole('button', { name: 'Mark concluded Salt and Starlight' })).toBeVisible()
  await expect(canvas.getByRole('button', { name: 'Concluded (2)' })).toHaveAttribute('aria-expanded', 'false')
  await expectTouchTargets(canvas, [
    'Prep The Drowned Crown', 'Start Session The Drowned Crown', 'Mark concluded Salt and Starlight', 'Concluded (2)', ...HEADER,
  ])
})
export const SeveralDark = dark(Several)
export const PhoneSeveral = onPhone(Several)
export const PhoneSeveralDark = onPhone(Several, 'dark')

/** The Concluded disclosure, open. */
export const ConcludedOpen = state(() => page(SEVERAL), async (canvas) => {
  const toggle = await canvas.findByRole('button', { name: 'Concluded (2)' })
  await userEvent.click(toggle)
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(canvas.getByRole('button', { name: 'Reopen Barrow of the Wren Queen' })).toBeVisible()
  await expect(canvas.getByRole('button', { name: 'Prep Barrow of the Wren Queen' })).toBeVisible()
  await expectTouchTargets(canvas, ['Reopen Barrow of the Wren Queen', 'Prep Barrow of the Wren Queen', 'Concluded (2)'])
})
export const ConcludedOpenDark = dark(ConcludedOpen)

/** ID-10: 13 active campaigns show 12, and Show more campaigns reveals the rest. */
export const ShowMore = state(() => page(THIRTEEN), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Prep Campaign 12' })).toBeVisible()
  await expect(canvas.queryByRole('button', { name: 'Prep Campaign 13' })).toBeNull()
  const more = canvas.getByRole('button', { name: 'Show more campaigns' })
  await expectTouchTargets(canvas, ['Show more campaigns'])
  await userEvent.click(more)
  await expect(await canvas.findByRole('button', { name: 'Prep Campaign 13' })).toBeVisible()
})

/** Concluded only: no empty line, Begin anew, then the disclosure. */
export const ConcludedOnly = state(() => page([SEVERAL[4], SEVERAL[5]]), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Concluded (2)' })).toBeVisible()
  await expect(canvas.queryByText('Create your first campaign', { exact: false })).toBeNull()
  await expect(canvas.getByRole('heading', { name: 'Begin anew', level: 2 })).toBeVisible()
  await expectTouchTargets(canvas, ['Concluded (2)', ...HEADER, 'Create campaign'])
})
export const ConcludedOnlyDark = dark(ConcludedOnly)

/** With a campaign already selected, Continue without a campaign is offered. */
export const WithSelection: Story = {
  decorators: [
    (Story) => (
      <CampaignProvider restore={{ campaignId: 'cmp_1', conversationId: null }}>
        <Story />
      </CampaignProvider>
    ),
  ],
  beforeEach: stubFetch((url) => (url === '/campaigns/cmp_1' ? json(SEVERAL[0]) : page(SEVERAL))),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
    await expect(await canvas.findByRole('button', { name: 'Continue without a campaign' })).toBeVisible()
    await expectTouchTargets(canvas, ['Continue without a campaign', 'Prep The Drowned Crown', ...HEADER])
  },
}
export const WithSelectionDark = dark(WithSelection)

// ── Phone (agent-forge-harness-0rn) ──────────────────────────────────────────

export const PhoneBrandNew = onPhone(BrandNew)
export const PhoneCouldNotLoad = onPhone(CouldNotLoad)

// ── 30c PR-2: Your seats, and the player's tavern ────────────────────────────

const seat = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, campaign_id: id, campaign_name: name, alias: 'Brannoc', accepted_at: day(2), confirmed: true,
  tone: null, game_system: 'dnd5e', avatar_icon: 'castle', avatar_tone: 'gold', concluded: false,
  last_played_at: null, live: false, ...over,
})
const SEATS = [
  seat('cmp_s1', 'Gorath\u2019s Table', { live: true, tone: 'Mystery · Low magic', last_played_at: day(27) }),
  seat('cmp_s2', 'The Pale Orchard', { confirmed: false, accepted_at: day(28), alias: 'Ysolde' }),
  seat('cmp_s3', 'Lanterns at Low Tide', { last_played_at: '2026-09-04T12:00:00Z', avatar_tone: 'ember' }),
  seat('cmp_s4', 'The Last Ferry', { concluded: true, last_played_at: day(1) }),
]

/** A player's tavern: the same screen, the seat network, and a player's shell. */
function playerState(seats: () => Response | Promise<Response>, play: Play): Story {
  return {
    decorators: [withCampaigns, withShell({ screen: 'tavern', mode: 'sage', role: 'player' })],
    beforeEach: stubFetch(withSeats(() => json({}, 404), seats)),
    play: async ({ canvasElement }) => {
      const canvas = within(canvasElement)
      await expect(await canvas.findByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeVisible()
      await play(canvas)
    },
  }
}
const PLAYER_HEADER = ['New Campaign', 'Back to chat']

/** E-1 (ID-25): a player with no seat gets the panel; New Campaign is locked, never hidden. */
export const PlayerNoSeats = playerState(() => page([]), async (canvas) => {
  await expect(await canvas.findByRole('heading', { name: 'Campaigns are being built', level: 2 })).toBeVisible()
  await expect(canvas.getByRole('button', { name: 'New Campaign' })).toHaveAttribute('aria-disabled', 'true')
  await expect(canvas.queryByRole('heading', { name: 'Begin anew' })).toBeNull()
  await expectTouchTargets(canvas, ['Ask the Sage', ...PLAYER_HEADER])
})
export const PlayerNoSeatsDark = dark(PlayerNoSeats)
export const PhonePlayerNoSeats = onPhone(PlayerNoSeats)
export const PhonePlayerNoSeatsDark = onPhone(PlayerNoSeats, 'dark')

/** ID-17, ID-23, ID-24: a live table as plain text, a seat waiting on its GM, a quiet one and a concluded one. */
export const PlayerSeats = playerState(() => page(SEATS), async (canvas) => {
  await expect(await canvas.findByRole('heading', { name: 'Gorath\u2019s Table', level: 2 })).toBeVisible()
  await expect(canvas.getByText('Live now')).toBeVisible()
  await expect(canvas.getByText('Waiting for your GM to confirm your seat')).toBeVisible()
  await expect(canvas.getByText(/^Last played 4 September/)).toBeVisible()
  await expect(canvas.getByText('This table has concluded')).toBeVisible()
  // Nothing in a seat card is a link or a button: no table page exists yet.
  await expect(canvas.queryAllByRole('link')).toHaveLength(0)
  await expectTouchTargets(canvas, PLAYER_HEADER)
})
export const PlayerSeatsDark = dark(PlayerSeats)
export const PhonePlayerSeats = onPhone(PlayerSeats)
export const PhonePlayerSeatsDark = onPhone(PlayerSeats, 'dark')

export const PlayerSeatsLoading = playerState(() => pending(), async (canvas) => {
  await expect(canvas.getByText('Loading your seats…', { selector: 'p:not([role])' })).toBeVisible()
  await expectTouchTargets(canvas, PLAYER_HEADER)
})

export const PlayerSeatsCouldNotLoad = playerState(() => json({}, 503), async (canvas) => {
  await expect(await canvas.findByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(canvas.getByText("Couldn't load your seats", { selector: 'p:not([role])' })).toBeVisible()
  await expect(canvas.queryByRole('heading', { name: 'Campaigns are being built' })).toBeNull()
  await expectTouchTargets(canvas, ['Retry', ...PLAYER_HEADER])
})
export const PlayerSeatsCouldNotLoadDark = dark(PlayerSeatsCouldNotLoad)

/** A GM who also holds a seat at another table: its campaigns, then Your seats, then Begin anew. */
export const GmWithSeats: Story = {
  decorators: [withCampaigns],
  beforeEach: stubFetch(withSeats(() => page(SEVERAL), () => page(SEATS))),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByRole('button', { name: 'Prep The Drowned Crown' })).toBeVisible()
    await expect(await canvas.findByRole('heading', { name: 'Your seats', level: 2 })).toBeVisible()
    await expect(canvas.getByText('Live now')).toBeVisible()
    await expect(canvas.getByRole('heading', { name: 'Begin anew', level: 2 })).toBeVisible()
    await expectTouchTargets(canvas, ['Prep The Drowned Crown', ...HEADER, 'Create campaign'])
  },
}
export const GmWithSeatsDark = dark(GmWithSeats)
export const PhoneGmWithSeats = onPhone(GmWithSeats)
