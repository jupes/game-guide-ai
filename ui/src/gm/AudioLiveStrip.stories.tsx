import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { AudioLiveStrip } from './AudioLiveStrip'
import { PRESENCE_MIXED, PRESENCE_SETTLED, makeGuests, makeParticipant } from './audioFixtures'

const AMBIENCE = { slot: 'ambience', title: 'Tidewarden Chant', loop: true } as const
const ONE_SHOT = { slot: 'one_shot', title: 'Thunderclap', loop: false } as const

const meta = {
  title: 'GM/AudioLiveStrip',
  component: AudioLiveStrip,
  tags: ['autodocs'],
  args: {
    slots: [AMBIENCE],
    presence: PRESENCE_SETTLED,
    onStopAll: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ width: 620, maxWidth: '100%' }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof AudioLiveStrip>

export default meta
type Story = StoryObj<typeof meta>

/** One slot in use, and the Stop that clears everything (AUDIO-9). */
export const OneSlot: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByText('Tidewarden Chant')).toBeInTheDocument()
    await expect(canvas.getByText('Ambience · loops')).toBeInTheDocument()

    await userEvent.click(canvas.getByRole('button', { name: 'Stop all' }))
    await expect(args.onStopAll).toHaveBeenCalledTimes(1)
  },
}

/** AUDIO-1: at most two slots, one of each kind. */
export const BothSlots: Story = {
  args: { slots: [AMBIENCE, ONE_SHOT], presence: PRESENCE_MIXED },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getAllByRole('listitem')).toHaveLength(2)
    await expect(canvas.getByText('Thunderclap')).toBeInTheDocument()
    await expect(canvas.getByText('One-shot')).toBeInTheDocument()
  },
}

/** §12.2: the indicator is absent when nothing is live. */
export const NothingLive: Story = {
  args: { slots: [] },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.textContent?.trim()).toBe('')
  },
}

/** AUDIO-17: the strip stays, says why, and keeps Stop reachable (X-3). */
export const Reconnecting: Story = {
  args: { connection: 'reconnecting' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent('Reconnecting…')
    await expect(canvas.getByRole('button', { name: 'Stop all' })).toBeEnabled()
  },
}

/** Nothing known to be live, and no stream to ask — Stop is still one action. */
export const ReconnectingWithNothingKnown: Story = {
  args: { slots: [], connection: 'reconnecting' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('region', { name: 'Live table audio' })).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Stop all' })).toBeEnabled()
  },
}

/** AUDIO-21: participants by alias, guests only ever counted. */
export const PresenceWithGuestsAndPending: Story = {
  args: { presence: PRESENCE_MIXED },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(
      canvas.getByText("2 listening · 1 muted · Gorath and 1 guest haven't tapped in yet"),
    ).toBeInTheDocument()
  },
}

/** The record's own example: one enrolled player has not tapped in yet. */
export const PresenceOnePending: Story = {
  args: {
    presence: {
      participants: [
        makeParticipant('Ondrey', 'listening'),
        makeParticipant('Mira', 'listening'),
        makeParticipant('Sel', 'muted'),
        makeParticipant('Gorath', 'pending'),
      ],
      guests: makeGuests({ connected: 1, listening: 1 }),
    },
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText("3 listening · 1 muted · Gorath hasn't tapped in yet")).toBeInTheDocument()
  },
}

/** §10.2: the narrow layout's named list, each cue with its own Stop. */
export const NarrowWithNamedStops: Story = {
  args: { slots: [AMBIENCE, ONE_SHOT], presence: PRESENCE_MIXED, onStopSlot: fn() },
  decorators: [
    (Story) => (
      <div style={{ width: 360 }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await userEvent.click(canvas.getByRole('button', { name: 'Stop Thunderclap' }))
    await expect(args.onStopSlot).toHaveBeenCalledWith('one_shot')
    await expect(canvasElement.scrollWidth).toBeLessThanOrEqual(canvasElement.clientWidth + 1)
    await expect(
      canvas.getByRole('button', { name: 'Stop all' }).getBoundingClientRect().height,
    ).toBeGreaterThanOrEqual(44)
  },
}

export const DarkTavern: Story = {
  args: { slots: [AMBIENCE, ONE_SHOT], presence: PRESENCE_MIXED },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Stop all' })).toBeEnabled()
  },
}
