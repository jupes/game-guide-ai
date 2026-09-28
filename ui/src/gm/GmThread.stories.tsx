/**
 * The GM thread's three lanes (1kg.3.4), one story per thing a GM can meet in a
 * reopened thread. Every turn is built from a wire-valid timeline entry
 * (`threadFixtures.ts`), so a story cannot show a shape the server never sends.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { GmThread } from './GmThread'
import { turnsFromTimeline } from './gmTimeline'
import { LANE_COPY } from './laneState'
import { EDIT_ENTRY, OPAQUE_ENTRY, SOURCED_ANSWER, SPELL_ANSWER, chatEntry, toolEntry } from './threadFixtures'

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
      { kind: 'chat', key: 'live:1', prompt: 'Who runs the inn?', answer: { state: 'pending' } },
      { kind: 'chat', key: 'live:2', prompt: 'And the stables?', answer: { state: 'failed', message: 'The service is busy right now — try again in a moment.' } },
    ],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Consulting the tomes…')).toBeVisible()
    await expect(canvas.getByText('The service is busy right now — try again in a moment.')).toBeVisible()
  },
}

/** A spell entry hydrated here (a mode chip keeps the conversation) keeps its
 * usage suggestions, apart from the card (agent-forge-harness-0ru). */
export const SpellWithSuggestions: Story = {
  args: {
    turns: turnsFromTimeline([
      chatEntry({ entry_id: 'ent_1', mode: 'spell', prompt: 'What does Fireball do?', answer: SPELL_ANSWER }),
    ]),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Practical')).toBeVisible()
    await expect(canvas.getByText('Instantly roast a feast.')).toBeVisible()
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
