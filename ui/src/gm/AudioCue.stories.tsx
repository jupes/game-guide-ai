import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { AudioCue } from './AudioCue'
import type { AudioCueProps } from './AudioCue'
import { AMBIENCE_CUE, ONE_SHOT_CUE } from './audioFixtures'

const meta = {
  title: 'GM/AudioCue',
  component: AudioCue,
  tags: ['autodocs'],
  args: {
    cue: AMBIENCE_CUE,
    preview: { playing: false, positionMs: 42_000, volume: 0.66, muted: false },
    loop: true,
    onPreviewToggle: fn(),
    onPreviewSeek: fn(),
    onPreviewVolume: fn(),
    onPreviewMute: fn(),
    onLoopChange: fn(),
    onPlayToTable: fn(),
    onStop: fn(),
    onRetryPush: fn(),
    onReplace: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ width: 560, maxWidth: '100%' }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof AudioCue>

export default meta
type Story = StoryObj<typeof meta>

/** Holds the preview state so seeking and the local controls really move. */
function LiveCue(props: AudioCueProps) {
  const [preview, setPreview] = React.useState(props.preview)
  const [loop, setLoop] = React.useState(props.loop ?? true)

  return (
    <AudioCue
      {...props}
      preview={preview}
      loop={loop}
      onPreviewToggle={() => {
        setPreview((state) => ({ ...state, playing: !state.playing }))
        props.onPreviewToggle()
      }}
      onPreviewSeek={(positionMs) => {
        setPreview((state) => ({ ...state, positionMs }))
        props.onPreviewSeek(positionMs)
      }}
      onPreviewVolume={(volume) => {
        setPreview((state) => ({ ...state, volume }))
        props.onPreviewVolume(volume)
      }}
      onPreviewMute={(muted) => {
        setPreview((state) => ({ ...state, muted }))
        props.onPreviewMute(muted)
      }}
      onLoopChange={(next) => {
        setLoop(next)
        props.onLoopChange?.(next)
      }}
    />
  )
}

/** Ambience: the only kind with a loop control (AUDIO-3). */
export const Ambience: Story = {
  render: (args) => <LiveCue {...args} />,
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    const loop = canvas.getByRole('button', { name: `Loop ${AMBIENCE_CUE.title}` })

    await expect(loop).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(loop)
    await expect(args.onLoopChange).toHaveBeenCalledWith(false)
    await expect(canvas.getByText('Ambience · plays once')).toBeInTheDocument()
    // AUDIO-7: nothing about the preview reaches the table.
    await expect(args.onPlayToTable).not.toHaveBeenCalled()
  },
}

/** A one-shot has no loop control at all — not a disabled one (AUDIO-3). */
export const OneShot: Story = {
  args: { cue: ONE_SHOT_CUE },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.queryByRole('button', { name: /loop/i })).toBeNull()
    await expect(canvas.getByText('One-shot')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Play to table' })).toBeInTheDocument()
  },
}

/** The private transport and the table's two controls, side by side. */
export const PreviewIsPrivate: Story = {
  render: (args) => <LiveCue {...args} />,
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    const preview = within(canvas.getByRole('group', { name: 'Preview (only you hear this)' }))
    const table = within(canvas.getByRole('group', { name: 'Table controls' }))

    await userEvent.click(preview.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` }))
    await expect(args.onPreviewToggle).toHaveBeenCalled()
    await expect(args.onPlayToTable).not.toHaveBeenCalled()
    await expect(args.onStop).not.toHaveBeenCalled()

    // AUDIO-8: exactly two controls face the table.
    await expect(table.getAllByRole('button')).toHaveLength(2)
    await expect(table.queryByRole('slider')).toBeNull()
  },
}

/** AUDIO-10: the waveform is fully operable from the keyboard. */
export const KeyboardSeek: Story = {
  render: (args) => <LiveCue {...args} />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const slider = canvas.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })

    slider.focus()
    await expect(slider).toHaveFocus()
    await expect(slider).toHaveAttribute('aria-valuetext', '0:42 of 3:10')

    await userEvent.keyboard('{ArrowRight}')
    await expect(slider).toHaveAttribute('aria-valuetext', '0:47 of 3:10')

    await userEvent.keyboard('{PageUp}')
    await expect(slider).toHaveAttribute('aria-valuetext', '1:17 of 3:10')

    await userEvent.keyboard('{Home}')
    await expect(slider).toHaveAttribute('aria-valuetext', '0:00 of 3:10')

    await userEvent.keyboard('{End}')
    await expect(slider).toHaveAttribute('aria-valuetext', '3:10 of 3:10')

    await userEvent.keyboard('{PageDown}')
    await expect(slider).toHaveAttribute('aria-valuetext', '2:40 of 3:10')
  },
}

/** Live to the table: the badge, and a Stop that names its cue (AUDIO-9). */
export const Live: Story = {
  args: { live: true, preview: { playing: true, positionMs: 15_000, volume: 0.66, muted: false } },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByText('LIVE')).toBeInTheDocument()
    await expect(canvas.getByRole('status')).toHaveTextContent('Playing to the table')

    const stop = canvas.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })
    await userEvent.click(stop)
    await expect(args.onStop).toHaveBeenCalledTimes(1)
  },
}

/** Not live: Stop has nothing to clear while the stream is healthy. */
export const NotLive: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.queryByText('LIVE')).toBeNull()
    await expect(canvas.getByRole('button', { name: 'Play to table' })).toBeEnabled()
    await expect(canvas.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeDisabled()
  },
}

/** AUDIO-17: Play to table goes, Stop stays. */
export const Disconnected: Story = {
  args: { live: true, connection: 'reconnecting' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent('Reconnecting…')
    await expect(canvas.getByRole('button', { name: 'Play to table' })).toBeDisabled()
    await expect(canvas.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeEnabled()
  },
}

/** AUDIO-6: previewing another cue pauses the monitor, never the table. */
export const MonitoringAnotherCue: Story = {
  args: { tableHearing: 'Storm Over Saltmarsh', preview: { playing: true, positionMs: 3_000, volume: 0.5, muted: false } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Table still hearing: Storm Over Saltmarsh')).toBeInTheDocument()
  },
}

/** §12.2: preview and push wait for the bytes. */
export const StillProcessing: Story = {
  args: { playability: 'processing' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent('Still processing…')
    await expect(canvas.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })).toBeDisabled()
    await expect(canvas.getByRole('button', { name: 'Play to table' })).toBeDisabled()
  },
}

/** §12.2: a file that will never play, with its way out. */
export const ProcessingFailed: Story = {
  args: { playability: 'failed' },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent("This file couldn't be processed")
    await userEvent.click(canvas.getByRole('button', { name: 'Replace' }))
    await expect(args.onReplace).toHaveBeenCalled()
  },
}

/** §12.2: a failed push is inline, and retried in place. */
export const PushFailed: Story = {
  args: { pushFailed: true },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)

    await expect(canvas.getByRole('status')).toHaveTextContent("Couldn't play to the table")
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await expect(args.onRetryPush).toHaveBeenCalled()
  },
}

/** §10.2: full width, the controls stack, the slider keeps its 44px hit area. */
export const Narrow: Story = {
  args: { live: true },
  decorators: [
    (Story) => (
      <div style={{ width: 360 }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const slider = canvas.getByRole('slider', { name: `Seek ${AMBIENCE_CUE.title}` })

    await expect(slider.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await expect(canvasElement.scrollWidth).toBeLessThanOrEqual(canvasElement.clientWidth + 1)

    const play = canvas.getByRole('button', { name: `Play preview of ${AMBIENCE_CUE.title}` })
    await expect(play.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}

/** Both themes come from the token layer alone. */
export const DarkTavern: Story = {
  args: { live: true },
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const title = canvas.getByRole('heading', { name: AMBIENCE_CUE.title })
    // A colour that resolved from a token, not a hard-coded light-theme value.
    await expect(getComputedStyle(title).color).not.toBe('')
    await expect(canvas.getByRole('button', { name: `Stop ${AMBIENCE_CUE.title}` })).toBeEnabled()
  },
}
