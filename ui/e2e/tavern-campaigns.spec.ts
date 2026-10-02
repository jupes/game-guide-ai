/**
 * tavern-campaigns.spec.ts -- "Your Campaigns" in the built SPA
 * (agent-forge-harness-30c, PR-1).
 *
 * Two kinds of test, because this E2E stack has no database:
 *
 * 1. The real server, no mocks. `/campaigns` answers 503 here
 *    (`service/campaigns_api.py`), so the error state is the one real-server
 *    state. What it proves is the way in: the Landing CTA of a GM opens `/tavern`
 *    (focused, in-app arrival), and Back to chat leaves it. Repeated on a
 *    390x844 phone with no page-level horizontal scroll.
 * 2. The card states, through `page.route` on the `/campaigns` family only
 *    (precedent: `resilience.spec.ts`). Everything else hits the real service.
 *    The routed bodies are valid `CampaignSchema` objects, so the real bundle's
 *    own parsing is what reads them.
 *
 * A new file, not an edit to an existing spec: the existing ones only change
 * where they entered the workspace (one line each).
 *
 * Every test signs in as the SECOND account. The per-account sign-in budget is
 * 10 attempts per 300 s and is deliberately not raised for the E2E stack
 * (docker-compose.e2e.yml); the first account already spends most of it across
 * the suite, so three more attempts there would turn a late spec into a 429.
 */

import type { Page } from '@playwright/test'
import { expect, signIn, test } from './fixtures'

const PHONE = { width: 390, height: 844 }

/** Copied from `phone.spec.ts` (which stays unedited). */
async function expectNoPageOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement
    return root.scrollWidth - root.clientWidth
  })
  expect(overflow).toBeLessThanOrEqual(0)
}

test('Enter the Tavern takes a GM to /tavern, focused, and Back to chat leaves it', async ({ page, accounts }) => {
  await page.goto('/')
  await signIn(page, accounts[1])
  await page.getByRole('button', { name: 'Enter the Tavern' }).click()

  await expect(page).toHaveURL(/\/tavern$/)
  const heading = page.getByRole('heading', { name: 'Your Campaigns', level: 1 })
  await expect(heading).toBeVisible()
  // An in-app arrival focuses the heading (74j I-10); an empty read moves it nowhere else.
  await expect(heading).toBeFocused()

  // No database in this stack: the list read is a 503, and the screen says so.
  const errorLine = page.locator('p:not([role])', { hasText: "Couldn't load campaigns" })
  await expect(errorLine).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  // Begin anew is there even though the list failed.
  await expect(page.getByRole('heading', { name: 'Begin anew', level: 2 })).toBeVisible()

  await page.getByRole('button', { name: 'Back to chat' }).click()
  await expect(page).toHaveURL(/\/workspace$/)
})

test.describe('on a 390px phone', () => {
  test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

  test('the same entry fits the screen', async ({ page, accounts }) => {
    await page.goto('/')
    await signIn(page, accounts[1])
    await expectNoPageOverflow(page)
    await page.getByRole('button', { name: 'Enter the Tavern', exact: true }).click()

    await expect(page).toHaveURL(/\/tavern$/)
    await expect(page.getByRole('heading', { name: 'Your Campaigns', level: 1 })).toBeFocused()
    await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'New Campaign', exact: true })).toBeVisible()
    await expectNoPageOverflow(page)
  })
})

// ── The card states, through page.route ──────────────────────────────────────

const SCHEMA_VERSION = 1

interface RoutedCampaign {
  schema_version: number
  campaign_id: string
  name: string
  created_at: string
  updated_at: string
  archived_at: null
  concluded_at: string | null
  tone: string | null
  game_system: 'dnd5e'
  avatar_icon: string
  avatar_tone: 'ember' | 'gold'
  badge: 'live' | 'ready' | null
  seat_count: number
  last_activity_at: string
  last_played_at: null
  dormant: boolean
}

function routed(n: number, over: Partial<RoutedCampaign> = {}): RoutedCampaign {
  const id = String(n).padStart(2, '0')
  return {
    schema_version: SCHEMA_VERSION,
    campaign_id: `cmp_e2e${id}`,
    name: `Campaign ${id}`,
    created_at: '2026-08-01T12:00:00Z',
    updated_at: '2026-08-01T12:00:00Z',
    archived_at: null,
    concluded_at: null,
    tone: null,
    game_system: 'dnd5e',
    avatar_icon: 'sailing',
    avatar_tone: 'ember',
    badge: null,
    seat_count: 0,
    // Campaign 01 is the most recent, so it is the top card.
    last_activity_at: `2026-09-${String(30 - n).padStart(2, '0')}T12:00:00Z`,
    last_played_at: null,
    dormant: false,
    ...over,
  }
}

/** 14 active (one dormant, one live, one ready) on the first page, and 2 concluded on the second. */
const ACTIVE = Array.from({ length: 14 }, (_, i) => {
  const n = i + 1
  if (n === 5) return routed(n, { dormant: true })
  if (n === 2) return routed(n, { badge: 'live' })
  if (n === 3) return routed(n, { badge: 'ready', seat_count: 4 })
  return routed(n)
})
const CONCLUDED = [
  routed(15, { concluded_at: '2026-09-01T12:00:00Z' }),
  routed(16, { concluded_at: '2026-09-02T12:00:00Z' }),
]
const DORMANT = ACTIVE[4]

test('the tavern shows a GM their campaigns: order, paging, Concluded, locked Start Session and Prep', async ({
  page,
  accounts,
}) => {
  const reads: string[] = []
  const posts: string[] = []
  const tableSession: string[] = []
  page.on('request', (request) => {
    if (request.url().includes('/table-session')) tableSession.push(request.url())
  })

  // Only the `/campaigns` family is routed. The pathname is matched exactly
  // (the cursor rides in the query), so `/campaigns/{id}/conclude` and the
  // rest of the app still reach the real service.
  await page.route(
    (url) => url.pathname === '/campaigns',
    async (route) => {
      const request = route.request()
      if (request.method() !== 'GET') return route.continue()
      const cursor = new URL(request.url()).searchParams.get('cursor')
      reads.push(cursor ?? '')
      const body = cursor === null
        ? { schema_version: SCHEMA_VERSION, items: ACTIVE, next_cursor: 'c2' }
        : { schema_version: SCHEMA_VERSION, items: CONCLUDED, next_cursor: null }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    },
  )
  await page.route(
    (url) => url.pathname === `/campaigns/${DORMANT.campaign_id}/conclude`,
    async (route) => {
      posts.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`)
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...DORMANT, concluded_at: '2026-10-01T09:00:00Z' }),
      })
    },
  )

  await page.goto('/')
  await signIn(page, accounts[1])
  await page.getByRole('button', { name: 'Enter the Tavern' }).click()
  await expect(page).toHaveURL(/\/tavern$/)

  // Both pages are read on their own, and the top card's Prep takes focus.
  await expect(page.getByRole('button', { name: 'Prep Campaign 01', exact: true })).toBeFocused()
  expect(reads).toEqual(['', 'c2'])

  // 12 of 14 are shown; Show more campaigns reveals the rest, with no request.
  const preps = page.getByRole('button', { name: /^Prep Campaign/ })
  await expect(preps).toHaveCount(12)
  await page.getByRole('button', { name: 'Show more campaigns' }).click()
  await expect(preps).toHaveCount(14)
  await expect(page.getByRole('button', { name: 'Prep Campaign 13', exact: true })).toBeFocused()
  expect(reads).toEqual(['', 'c2'])

  // The badges and the dormant card read as such.
  await expect(page.getByText('LIVE', { exact: true })).toBeVisible()
  await expect(page.getByText('READY', { exact: true })).toBeVisible()
  await expect(page.getByText('4 players', { exact: true })).toBeVisible()
  await expect(page.getByText(/Dormant · no activity since/)).toBeVisible()

  // Concluded (2) is a disclosure.
  const toggle = page.getByRole('button', { name: 'Concluded (2)', exact: true })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('button', { name: 'Reopen Campaign 15', exact: true })).toBeHidden()
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('button', { name: 'Reopen Campaign 15', exact: true })).toBeVisible()

  // Marking the dormant campaign concluded moves it to Concluded (3).
  await page.getByRole('button', { name: 'Mark concluded Campaign 05', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Concluded (3)', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Concluded (3)', exact: true })).toBeFocused()
  expect(posts).toEqual([`POST /campaigns/${DORMANT.campaign_id}/conclude`])

  // Start Session is locked, and pressing it makes no request of any kind.
  const start = page.getByRole('button', { name: 'Start Session Campaign 01', exact: true })
  await expect(start).toHaveAttribute('aria-disabled', 'true')
  const before: string[] = []
  const record = (request: { url: () => string }): void => {
    before.push(request.url())
  }
  page.on('request', record)
  // Playwright treats aria-disabled as not enabled, so the press is forced: it is the press itself under test.
  await start.click({ force: true })
  await page.waitForTimeout(250)
  page.off('request', record)
  expect(before).toEqual([])
  expect(tableSession).toEqual([])

  // Prep on the top card opens that campaign's GM channel.
  await page.getByRole('button', { name: 'Prep Campaign 01', exact: true }).click()
  await expect(page).toHaveURL(/\/workspace#campaign=cmp_e2e01$/)
  const channels = page.getByRole('navigation', { name: 'Channels' })
  await expect(channels.getByRole('button', { name: 'GM' })).toHaveAttribute('aria-pressed', 'true')
})
