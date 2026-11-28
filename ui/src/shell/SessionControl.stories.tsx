/**
 * SessionControl -- every state of the table-session control, in both themes
 * (agent-forge-harness-1kg.2.5.3; brief T4-8). Each story passes the axe gate,
 * checks its controls' 44 px targets and that it holds exactly one live region.
 * The phone stories check 0rn's rule: one column under 600 px, no overflow.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, within } from 'storybook/test'

import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { SessionControl } from './SessionControl'
import type { TableSessionValue, TableSessionView } from './tableSession'

const LIVE: TableSessionView = {
  sessionId: 'tss_1',
  campaignId: 'cmp_A',
  state: 'live',
  startedAt: '2026-09-29T19:00:00Z',
  endsAt: '2026-09-30T07:00:00Z',
  endedAt: null,
}
const LIVE_SESSION = { sessionId: LIVE.sessionId, campaignId: LIVE.campaignId, revealEpoch: 0, audioEpoch: 0 }
const ENDED: TableSessionView = { ...LIVE, state: 'ended', endedAt: '2026-09-29T23:30:00Z' }
const LIVE_REGIONS = '[role="status"],[role="alert"],[role="log"],[aria-live]'

function valueOf(overrides: Partial<TableSessionValue>): TableSessionValue {
  return {
    state: 'none',
    session: null,
    pending: null,
    endRetrying: false,
    problem: null,
    liveSession: null,
    start: fn(async () => 'started' as const),
    end: fn(async () => 'ended' as const),
    retry: fn(async () => 'skipped' as const),
    ...overrides,
  }
}

const meta = {
  title: 'Shell/SessionControl',
  component: SessionControl,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
} satisfies Meta<typeof SessionControl>

export default meta
type Story = StoryObj<typeof meta>

type State = 'None' | 'Starting' | 'StartFailed' | 'Live' | 'EndingRetrying' | 'Expired' | 'Ended' | 'LiveElsewhere' | 'Throttled'

/** Each state: what the provider says, the text it shows, the controls it offers. */
const STATES: Record<State, readonly [Partial<TableSessionValue>, RegExp, readonly string[]]> = {
  None: [{}, /No session running/, ['Start session']],
  Starting: [{ pending: 'start' }, /Starting…/, ['Start session']],
  StartFailed: [{ problem: { kind: 'start_failed' } }, /Couldn't start the session/, ['Retry']],
  Live: [{ state: 'live', session: LIVE, liveSession: LIVE_SESSION }, /Live until/, ['End session', 'Share table']],
  EndingRetrying: [
    { state: 'live', session: LIVE, liveSession: LIVE_SESSION, pending: 'end', endRetrying: true },
    /Couldn't end the session yet/,
    ['End session'],
  ],
  Expired: [{ state: 'live', session: LIVE }, /Session end time has passed/, ['End session']],
  Ended: [{ state: 'ended', session: ENDED }, /The last session ended at/, ['Start session']],
  LiveElsewhere: [{ problem: { kind: 'live_elsewhere' } }, /live in another campaign/, ['Start session']],
  Throttled: [{ problem: { kind: 'throttled', retryAfterS: 60 } }, /Try again in 60 seconds/, ['Retry']],
}

function story(state: State, theme: 'light' | 'dark'): Story {
  const [overrides, text, buttons] = STATES[state]
  return {
    args: { value: valueOf(overrides) },
    globals: { theme },
    play: async ({ canvasElement }) => {
      await expectTheme(theme)
      const canvas = within(canvasElement)
      await expect(canvas.getByText(text)).toBeVisible()
      await expect(canvasElement.querySelectorAll(LIVE_REGIONS)).toHaveLength(1)
      await expectTouchTargets(canvas, buttons)
    },
  }
}

export const None: Story = story('None', 'light')
export const NoneDark: Story = story('None', 'dark')
export const Starting: Story = story('Starting', 'light')
export const StartingDark: Story = story('Starting', 'dark')
export const StartFailed: Story = story('StartFailed', 'light')
export const StartFailedDark: Story = story('StartFailed', 'dark')
export const Live: Story = story('Live', 'light')
export const LiveDark: Story = story('Live', 'dark')
export const EndingRetrying: Story = story('EndingRetrying', 'light')
export const EndingRetryingDark: Story = story('EndingRetrying', 'dark')
export const Expired: Story = story('Expired', 'light')
export const ExpiredDark: Story = story('Expired', 'dark')
export const Ended: Story = story('Ended', 'light')
export const EndedDark: Story = story('Ended', 'dark')
export const LiveElsewhere: Story = story('LiveElsewhere', 'light')
export const LiveElsewhereDark: Story = story('LiveElsewhere', 'dark')
export const Throttled: Story = story('Throttled', 'light')
export const ThrottledDark: Story = story('Throttled', 'dark')

function phone(theme: 'light' | 'dark'): Story {
  return {
    ...atViewport('phone375', theme),
    args: { value: valueOf(STATES.Throttled[0]) },
    play: async ({ canvasElement }) => {
      await expectViewport('phone375')
      await expectTheme(theme)
      const canvas = within(canvasElement)
      const text = canvas.getByText(STATES.Throttled[1]).getBoundingClientRect()
      const retry = canvas.getByRole('button', { name: 'Retry' }).getBoundingClientRect()
      const group = canvas.getByRole('group', { name: 'Table session' }).getBoundingClientRect()
      await expect(retry.top).toBeGreaterThanOrEqual(text.bottom)
      await expect(Math.abs(retry.width - group.width)).toBeLessThan(1)
      await expectTouchTargets(canvas, ['Retry'])
      await expectNoPageOverflow()
    },
  }
}

/** 0rn: under 600 px the text and the action stack in one column. */
export const Phone: Story = phone('light')
export const PhoneDark: Story = phone('dark')
