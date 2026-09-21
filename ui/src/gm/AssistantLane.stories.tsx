/**
 * Every row of §3.4, as a story with an interaction test (1kg.3.2).
 *
 * The lane is the surface a GM reads while money is being spent, so each state
 * is demonstrable on its own rather than reachable only by waiting for a real
 * run. The 30 s clock and the hydration flags are props precisely so that
 * `Still working…` and RAIL-21's `unknown` can be shown here in a browser and
 * asserted in a test without a stopwatch.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { AssistantLane } from './AssistantLane'
import { LANE_COPY } from './laneState'
import type { LaneTimer } from './laneState'
import {
  ALL_ENABLED,
  DEFAULT_AVAILABILITY,
  SUGGESTIONS,
  cardResult,
  documentResult,
  errorInfo,
  mediaResult,
  toolInvocation,
} from './laneFixtures'

/** RAIL-15's 30 s, collapsed to a tick so the state is demonstrable. */
const instantTimer: LaneTimer = {
  schedule(_ms, run) {
    const handle = setTimeout(run, 0)
    return () => {
      clearTimeout(handle)
    }
  },
}

const meta = {
  title: 'Aetheril/AssistantLane',
  component: AssistantLane,
  tags: ['autodocs'],
  args: {
    invocation: toolInvocation(),
    availability: ALL_ENABLED,
    sourceEntryId: 'ent_1',
    onOpenDocument: fn(),
    onArmSuggestion: fn(),
    onCancel: fn(),
    onRetry: fn(),
    onEditBrief: fn(),
    onCheckAgain: fn(),
    onRunAgain: fn(),
  },
  parameters: { layout: 'padded' },
} satisfies Meta<typeof AssistantLane>

export default meta
type Story = StoryObj<typeof meta>

// ── Working (RAIL-15) ────────────────────────────────────────────────────────

export const Working: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Writing the dossier…')
    await expect(canvas.getByText('NPC')).toBeVisible()

    await userEvent.click(canvas.getByRole('button', { name: 'Cancel' }))
    await expect(args.onCancel).toHaveBeenCalledTimes(1)
  },
}

export const StillWorking: Story = {
  args: { timer: instantTimer },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(await canvas.findByText(LANE_COPY.stillWorking)).toBeVisible()
  },
}

export const Cancelling: Story = {
  args: { invocation: toolInvocation({ cancel_requested: true }) },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent(LANE_COPY.cancelling)
    // RAIL-23: the run may still be billing, so Cancel stays reachable.
    await expect(canvas.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  },
}

// ── Unknown (RAIL-21) ────────────────────────────────────────────────────────

export const CheckingAfterReload: Story = {
  args: { hydrated: true },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Checking on NPC…')
    await expect(canvas.queryByRole('button', { name: 'Try again' })).toBeNull()

    await userEvent.click(canvas.getByRole('button', { name: 'Check again' }))
    await expect(args.onCheckAgain).toHaveBeenCalledTimes(1)
    // It polls; it never re-runs (X-1).
    await expect(args.onRetry).not.toHaveBeenCalled()
  },
}

// ── Done (RAIL-17, CANVAS-3) ─────────────────────────────────────────────────

export const DoneWithDocument: Story = {
  args: { invocation: toolInvocation({ status: 'done', result: documentResult() }) },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('NPC Dossier · saved to NPCs')).toBeVisible()
    await expect(canvas.getByRole('status')).toHaveTextContent('NPC finished')
    await expect(args.onOpenDocument).not.toHaveBeenCalled()

    await userEvent.click(canvas.getByRole('button', { name: /Open in canvas/ }))
    await expect(args.onOpenDocument).toHaveBeenCalledTimes(1)
  },
}

export const DoneWithCard: Story = {
  args: {
    invocation: toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Drowned Thing')).toBeVisible()
    await expect(canvas.getByText('grapple pressure')).toBeVisible()
  },
}

export const DoneWithSuggestions: Story = {
  args: {
    invocation: toolInvocation({
      status: 'done',
      result: documentResult({ suggestions: SUGGESTIONS }),
    }),
  },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    for (const suggestion of SUGGESTIONS) {
      await expect(canvas.getByRole('button', { name: suggestion.label })).toBeVisible()
    }

    await userEvent.click(canvas.getByRole('button', { name: 'Build an encounter around it' }))
    // RAIL-8: it ARMS. The command and brief go back; nothing runs.
    await expect(args.onArmSuggestion).toHaveBeenCalledWith({
      toolId: 'encounter',
      command: '/encounter',
      brief: 'an ambush at the crossing',
      sourceEntryId: 'ent_1',
    })
  },
}

export const SuggestionsForDisabledTools: Story = {
  args: {
    availability: DEFAULT_AVAILABILITY,
    invocation: toolInvocation({
      status: 'done',
      result: documentResult({
        suggestions: [{ tool_id: 'portrait', label: 'Portrait', icon: 'image', brief: null }, SUGGESTIONS[1]],
      }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'Portrait' })).toBeNull()
    await expect(canvas.getByRole('button', { name: 'Three hooks' })).toBeVisible()
  },
}

export const FinishedBeforeCancel: Story = {
  args: {
    invocation: toolInvocation({ status: 'done', cancel_requested: true, result: documentResult() }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(LANE_COPY.lateFinish)).toBeVisible()
    await expect(canvas.getByRole('button', { name: /Open in canvas/ })).toBeVisible()
  },
}

// ── Failures (RAIL-18 to RAIL-20) ────────────────────────────────────────────

export const FailedRetryable: Story = {
  args: { invocation: toolInvocation({ status: 'failed', error: errorInfo({ retryable: true }) }) },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent("Aetheril can't reach its library right now.")
    await userEvent.click(canvas.getByRole('button', { name: 'Try again' }))
    await expect(args.onRetry).toHaveBeenCalledTimes(1)
  },
}

export const FailedFinal: Story = {
  args: {
    invocation: toolInvocation({
      status: 'failed',
      error: errorInfo({ code: 'brief_too_long', message: 'That brief is too long.', retryable: false }),
    }),
  },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'Try again' })).toBeNull()
    await userEvent.click(canvas.getByRole('button', { name: 'Edit brief' }))
    await expect(args.onEditBrief).toHaveBeenCalledTimes(1)
  },
}

export const ThrottledPerUser: Story = {
  args: {
    invocation: toolInvocation({
      status: 'failed',
      error: errorInfo({ code: 'throttled_user', message: 'Too many.', retryable: true, retry_after_s: 45 }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent("That's a lot at once — try again in 45 seconds")
    await expect(canvas.getByRole('button', { name: 'Try again' })).toBeVisible()
  },
}

export const ThrottledDailyCap: Story = {
  args: {
    invocation: toolInvocation({
      status: 'failed',
      error: errorInfo({ code: 'throttled_daily', message: 'Spent.', retryable: false }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent(LANE_COPY.dailyCap)
    // RAIL-20: no retry is offered, because there is no window to wait out.
    await expect(canvas.queryAllByRole('button')).toHaveLength(0)
  },
}

export const ThrottledUnmarked: Story = {
  args: {
    invocation: toolInvocation({
      status: 'failed',
      // A code this client does not know — the platform's own 429, not one of
      // the two the Workbench marks. Codes carry no digits (wire contract).
      error: errorInfo({ code: 'too_many_requests', message: 'Slow down.', retryable: true }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    // An unmarked 429 is RAIL-18, not an invented window.
    await expect(canvas.getByRole('status')).toHaveTextContent('Slow down.')
    await expect(canvas.getByRole('button', { name: 'Try again' })).toBeVisible()
  },
}

// ── Cancelled and unknown results (RAIL-22, RAIL-24) ─────────────────────────

export const Cancelled: Story = {
  args: { invocation: toolInvocation({ status: 'cancelled' }) },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent(LANE_COPY.cancelled)
    await userEvent.click(canvas.getByRole('button', { name: 'Run again' }))
    await expect(args.onRunAgain).toHaveBeenCalledTimes(1)
  },
}

export const UnsupportedResultKind: Story = {
  args: {
    invocation: toolInvocation({ tool_id: 'portrait', status: 'done', result: mediaResult() }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(LANE_COPY.unsupportedResult)).toBeVisible()
    // X-8: never a borrowed card.
    await expect(canvas.queryByText('NPC')).toBeNull()
  },
}

export const UnreadableEntry: Story = {
  args: { invocation: null },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(LANE_COPY.newerVersion)).toBeVisible()
    await expect(canvas.queryAllByRole('button')).toHaveLength(0)
  },
}

// ── Both themes ──────────────────────────────────────────────────────────────

export const DarkTavern: Story = {
  globals: { theme: 'dark' },
  args: {
    invocation: toolInvocation({
      status: 'done',
      result: documentResult({ suggestions: SUGGESTIONS }),
    }),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('NPC Dossier · saved to NPCs')).toBeVisible()
    await expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  },
}
