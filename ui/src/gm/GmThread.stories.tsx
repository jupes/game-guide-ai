/**
 * The GM thread's three lanes (1kg.3.4), one story per thing a GM can meet in a
 * reopened thread. Every turn is built from a wire-valid timeline entry
 * (`threadFixtures.ts`), so a story cannot show a shape the server never sends.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, within } from 'storybook/test'

import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { expectTouchTarget } from '../../.storybook/touchTarget'
import { GmThread } from './GmThread'
import { collapseSessionSpans, turnsFromTimeline } from './gmTimeline'
import { LANE_COPY } from './laneState'
import {
  DIVIDER_ENTRY,
  EDIT_ENTRY,
  END_DIVIDER_ENTRY,
  OPAQUE_ENTRY,
  QUIET_SESSION,
  SOURCED_ANSWER,
  chatEntry,
  toolEntry,
} from './threadFixtures'

const meta = {
  title: 'Aetheril/GmThread',
  component: GmThread,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    turns: turnsFromTimeline([
      chatEntry({ entry_id: 'ent_1' }),
      chatEntry({ entry_id: 'ent_2', prompt: "How does a basilisk's gaze work?", answer: SOURCED_ANSWER }),
      toolEntry({ entry_id: 'ent_3' }),
    ]),
  },
  decorators: [
    (Story) => (
      <div style={{ maxWidth: '48rem', display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof GmThread>

export default meta
type Story = StoryObj<typeof meta>

/** Narration, then the lane beneath it: a card compact, a disclaimer, citations compact. */
export const ThreeLanes: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Give me a drowned guardian for the marsh.')).toBeVisible()
    await expect(canvas.getByText('AC 16')).toBeVisible()
    await expect(canvas.getByText(/Creative — may include invented content/)).toBeVisible()
    await expect(canvas.getByText('1 source')).toBeVisible()
    await expect(canvas.getByText('Checking on Monster…')).toBeVisible()
    // Reading order is visual order: each exchange's turn comes before its lane.
    const [first] = canvasElement.querySelectorAll('.gm-thread__exchange')
    const turn = within(first as HTMLElement).getByText('Give me a drowned guardian for the marsh.')
    const lane = (first as HTMLElement).querySelector('.assistant-lane') as HTMLElement
    await expect(turn.compareDocumentPosition(lane) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  },
}

/** RAIL-9: an empty optional brief shows the tool's blurb in italics. */
export const RecapWithNoBrief: Story = {
  args: { turns: turnsFromTimeline([toolEntry({ brief: '' }, { tool_id: 'recap' })]) },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector('.gm-thread__tool')).toHaveTextContent('Recap')
    await expect(canvasElement.querySelector('.chat-message--dm em')).toHaveTextContent('Recap the session so far')
  },
}

/** A turn in flight, and one that failed. */
export const LiveStages: Story = {
  args: {
    turns: [
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'pending' }, mode: 'gm' },
      {
        kind: 'chat',
        key: 'live:2',
        prompt: 'And the stables?',
        answer: { state: 'failed', message: 'The service is busy right now — try again in a moment.' },
        mode: 'gm',
      },
    ],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Consulting the tomes…')).toBeVisible()
    await expect(canvas.getByText('The service is busy right now — try again in a moment.')).toBeVisible()
  },
}

/** What this thread cannot draw yet keeps its place and says so (RAIL-24, X-8). */
export const Placeholders: Story = {
  args: { turns: turnsFromTimeline([OPAQUE_ENTRY, EDIT_ENTRY]) },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(LANE_COPY.newerVersion)).toBeVisible()
    await expect(canvas.getByText(LANE_COPY.unsupportedResult)).toBeVisible()
  },
}

export const DarkTavern: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('AC 16')).toBeVisible()
    await expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  },
}

// ── Session dividers (1kg.3.5) ───────────────────────────────────────────────
// Each story runs in both themes through the addon-a11y gate. A divider is
// text between the exchanges (I-11): no role, no live region, not in the Tab
// order, and a `<time>` for every instant it names.

/** A played session (start, a turn, end), a quiet one collapsed to one line, then play resumes. */
const SESSIONS = collapseSessionSpans(
  turnsFromTimeline([
    DIVIDER_ENTRY,
    chatEntry({ entry_id: 'ent_1' }),
    END_DIVIDER_ENTRY,
    ...QUIET_SESSION,
    chatEntry({ entry_id: 'ent_2', prompt: "How does a basilisk's gaze work?", answer: SOURCED_ANSWER }),
  ]),
)

/** The dividers drawn, in order, each checked for what I-11 requires. */
async function expectDividers(canvasElement: HTMLElement, boundaries: readonly string[]): Promise<HTMLElement[]> {
  const found = [...canvasElement.querySelectorAll<HTMLElement>('.gm-thread__divider')]
  await expect(found.map((divider) => divider.dataset.boundary)).toEqual(boundaries)
  for (const divider of found) {
    await expect(divider).toBeVisible()
    await expect(divider.tabIndex).toBe(-1)
    await expect(divider.hasAttribute('role')).toBe(false)
    await expect(divider.querySelector('[aria-live],[role]')).toBeNull()
    await expect(divider.closest('.gm-thread__exchange')).toBeNull()
    for (const time of divider.querySelectorAll('time')) {
      await expect(Number.isFinite(Date.parse(time.dateTime))).toBe(true)
    }
  }
  return found
}

/** A played session between its two dividers, then a quiet week drawn as one line. */
export const WithSessionDividers: Story = {
  args: { turns: SESSIONS },
  globals: { theme: 'light' },
  play: async ({ canvasElement }) => {
    await expectTheme('light')
    const canvas = within(canvasElement)
    const [start, end, span] = await expectDividers(canvasElement, ['start', 'end', 'span'])
    await expect(within(start).getByText(/Session started/)).toBeVisible()
    await expect(within(end).getByText(/Session ended/)).toBeVisible()
    await expect(within(span).getByText(/Session played/)).toBeVisible()
    await expect(span.querySelectorAll('time')).toHaveLength(2)
    // Reading order is visual order: the played session's turn sits between its dividers.
    const turn = canvas.getByText('Give me a drowned guardian for the marsh.')
    await expect(start.compareDocumentPosition(turn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await expect(turn.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  },
}

export const WithSessionDividersDark: Story = {
  ...WithSessionDividers,
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expectDividers(canvasElement, ['start', 'end', 'span'])
  },
}

/** I-10: a session that left nothing in this thread is one quiet line, "… to …". */
export const AQuietSession: Story = {
  args: { turns: collapseSessionSpans(turnsFromTimeline([...QUIET_SESSION])) },
  globals: { theme: 'light' },
  play: async ({ canvasElement }) => {
    await expectTheme('light')
    const [span] = await expectDividers(canvasElement, ['span'])
    // The dash is drawn and hidden from assistive technology; "to" is read and not drawn.
    await expect(within(span).getByText('–')).toHaveAttribute('aria-hidden', 'true')
    const to = within(span).getByText('to')
    await expect(to.getBoundingClientRect().width).toBeLessThanOrEqual(1)
  },
}

export const AQuietSessionDark: Story = {
  ...AQuietSession,
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expectDividers(canvasElement, ['span'])
  },
}

/** A thread whose oldest drawn item is a divider, with older turns still to load:
 * the divider is where Load earlier's hand-off lands (1kg.3.7), so it takes
 * focus from a script (tabIndex -1) and shows the ring, but never from Tab. */
export const DividerFirst: Story = {
  args: {
    turns: turnsFromTimeline([DIVIDER_ENTRY, chatEntry({ entry_id: 'ent_1' })]),
    hasEarlier: true,
    onLoadEarlier: fn(),
  },
  globals: { theme: 'light' },
  play: async ({ canvasElement }) => {
    await expectTheme('light')
    const [divider] = await expectDividers(canvasElement, ['start'])
    const button = within(canvasElement).getByRole('button', { name: 'Load earlier' })
    await expect(button.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await expect(canvasElement.querySelector('.gm-thread__divider, .gm-thread__exchange')).toBe(divider)
    divider.focus()
    await expect(document.activeElement).toBe(divider)
  },
}

export const DividerFirstDark: Story = {
  ...DividerFirst,
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expectDividers(canvasElement, ['start'])
  },
}

/** agent-forge-harness-0rn: on a 320px phone the divider's row wraps and its
 * label breaks anywhere, so the page never scrolls sideways; Load earlier keeps
 * the 44px touch floor. */
async function playOnAPhone(canvasElement: HTMLElement, theme: 'light' | 'dark'): Promise<void> {
  await expectViewport('phone320')
  await expectTheme(theme)
  const [start, span] = await expectDividers(canvasElement, ['start', 'span'])
  await expectNoPageOverflow()
  for (const divider of [start, span]) {
    await expect(divider.scrollWidth).toBeLessThanOrEqual(divider.clientWidth)
  }
  // The long "… to …" label wraps onto more than one line rather than overflowing.
  const label = span.querySelector('.gm-thread__divider-label') as HTMLElement
  const lineHeight = Number.parseFloat(getComputedStyle(label).lineHeight)
  await expect(label.getBoundingClientRect().height).toBeGreaterThan(lineHeight * 1.5)
  await expectTouchTarget(within(canvasElement), 'Load earlier')
}

const PHONE_TURNS = collapseSessionSpans(
  turnsFromTimeline([DIVIDER_ENTRY, chatEntry({ entry_id: 'ent_1', answer: null }), ...QUIET_SESSION]),
)

export const DividersOnAPhone320: Story = {
  args: { turns: PHONE_TURNS, hasEarlier: true, onLoadEarlier: fn() },
  ...atViewport('phone320'),
  play: async ({ canvasElement }) => playOnAPhone(canvasElement, 'light'),
}

export const DividersOnAPhone320Dark: Story = {
  args: { turns: PHONE_TURNS, hasEarlier: true, onLoadEarlier: fn() },
  ...atViewport('phone320', 'dark'),
  play: async ({ canvasElement }) => playOnAPhone(canvasElement, 'dark'),
}
