import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { AudioConsentPrompt } from './AudioConsentPrompt'
import type { AudioConsentPromptProps } from './AudioConsentPrompt'

const meta = {
  title: 'Table/AudioConsentPrompt',
  component: AudioConsentPrompt,
  tags: ['autodocs'],
  args: {
    accepted: false,
    muted: false,
    onAccept: fn(),
    onMuteChange: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ width: 360, maxWidth: '100%' }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof AudioConsentPrompt>

export default meta
type Story = StoryObj<typeof meta>

/** AUDIO-18: consent lives in this component's caller, in memory, for one page load. */
function TableSound(props: AudioConsentPromptProps) {
  const [accepted, setAccepted] = React.useState(props.accepted)
  const [muted, setMuted] = React.useState(props.muted)

  return (
    <AudioConsentPrompt
      {...props}
      accepted={accepted}
      muted={muted}
      onAccept={() => {
        setAccepted(true)
        props.onAccept()
      }}
      onMuteChange={(next) => {
        setMuted(next)
        props.onMuteChange(next)
      }}
    />
  )
}

/** AUDIO-19: the small banner a joining client sees at once. */
export const Banner: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('button', { name: 'Tap to enable sound' })).toBeInTheDocument()
    await expect(canvas.queryByText('Your GM is playing something')).toBeNull()
  },
}

/** AUDIO-19: a cue arrived before the tap, so the full prompt appears. */
export const CueWaiting: Story = {
  args: { cueWaiting: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Your GM is playing something')).toBeInTheDocument()
  },
}

/** One tap, and the gate becomes the player's only control (AUDIO-18, AUDIO-20). */
export const OneTapThenMute: Story = {
  args: { cueWaiting: true },
  render: (args) => <TableSound {...args} />,
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await userEvent.click(canvas.getByRole('button', { name: /Tap to enable sound/ }))
    await expect(args.onAccept).toHaveBeenCalledTimes(1)

    const mute = await canvas.findByRole('button', { name: 'Mute' })
    await expect(mute).toHaveAttribute('aria-pressed', 'false')
    await expect(canvas.queryByRole('button', { name: /Tap to enable sound/ })).toBeNull()

    await userEvent.click(mute)
    await expect(args.onMuteChange).toHaveBeenCalledWith(true)
    await expect(canvas.getByRole('button', { name: 'Mute' })).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(canvas.getByRole('button', { name: 'Mute' }))
    await expect(args.onMuteChange).toHaveBeenCalledWith(false)
    await expect(canvas.getByRole('button', { name: 'Mute' })).toHaveAttribute('aria-pressed', 'false')
  },
}

export const Accepted: Story = {
  args: { accepted: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getAllByRole('button')).toHaveLength(1)
    await expect(canvas.getByRole('button', { name: 'Mute' })).toBeInTheDocument()
  },
}

export const Muted: Story = {
  args: { accepted: true, muted: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Mute' })).toHaveAttribute('aria-pressed', 'true')
  },
}

/** §12.2, audio (player). */
export const LoadingSound: Story = {
  args: { accepted: true, status: 'loading' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Loading sound…')
  },
}

/** §12.2: mute is available even when nothing can play. */
export const CannotPlay: Story = {
  args: { accepted: true, status: 'error' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent("Your browser can't play this sound")
    await expect(canvas.getByRole('button', { name: 'Mute' })).toBeEnabled()
  },
}

/** §10.2: the player table view is usable from 320px. */
export const Narrow: Story = {
  args: { cueWaiting: true },
  decorators: [
    (Story) => (
      <div style={{ width: 320 }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const tap = canvas.getByRole('button', { name: /Tap to enable sound/ })

    await expect(tap.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await expect(canvasElement.scrollWidth).toBeLessThanOrEqual(canvasElement.clientWidth + 1)
  },
}

export const DarkTavern: Story = {
  args: { accepted: true },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Mute' })).toBeEnabled()
  },
}
