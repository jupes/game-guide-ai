import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { REGISTRY, toolAvailability } from './registry'
import { ToolComposer } from './ToolComposer'
import type { ToolComposerProps } from './ToolComposer'
import type { ToolId } from './contracts'

const ALL_ENABLED = toolAvailability({ image_generation: true, audio_cues: true })
const DEFAULTS = toolAvailability({ image_generation: false, audio_cues: false })

/** The composer is controlled; this holds the draft and the pins for it. */
function LiveComposer(props: ToolComposerProps) {
  const [draft, setDraft] = React.useState(props.draft)
  const [pins, setPins] = React.useState<readonly ToolId[]>(props.pins)
  return (
    <ToolComposer
      {...props}
      draft={draft}
      onDraftChange={(next) => {
        setDraft(next)
        props.onDraftChange(next)
      }}
      pins={pins}
      onPinsChange={(next) => {
        setPins(next)
        props.onPinsChange(next)
      }}
    />
  )
}

const meta = {
  title: 'GM/ToolComposer',
  component: ToolComposer,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    pins: [...REGISTRY.default_pinned],
    onPinsChange: fn(),
    availability: ALL_ENABLED,
    draft: '',
    onDraftChange: fn(),
    onInvoke: fn(),
    onChat: fn(),
    announceDelayMs: 0,
  },
  render: (args) => <LiveComposer {...args} />,
} satisfies Meta<typeof ToolComposer>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** AE-1: a rail tap arms the composer and sends nothing. */
export const RailTapArms: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: /npc/i }))
    const field = canvas.getByRole('textbox', { name: 'Message' })
    await expect(field).toHaveValue('/npc ')
    await expect(field).toHaveFocus()
    await expect(args.onInvoke).not.toHaveBeenCalled()
  },
}

/** AE-8 and SLASH-11: one Enter completes the command, the next one runs it. */
export const TwoPresses: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    const field = canvas.getByRole('textbox', { name: 'Message' })
    await userEvent.click(field)
    await userEvent.keyboard('/rec')
    await expect(canvas.getByRole('listbox', { name: 'Tools' })).toBeInTheDocument()
    await userEvent.keyboard('{Enter}')
    await expect(field).toHaveValue('/recap ')
    await expect(args.onInvoke).not.toHaveBeenCalled()
    await userEvent.keyboard('{Enter}')
    await expect(args.onInvoke).toHaveBeenCalledTimes(1)
  },
}

/** AE-3: a required tool with an empty brief shows its hint and sends nothing. */
export const BriefRequired: Story = {
  args: { draft: '/npc ' },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('textbox', { name: 'Message' }))
    await userEvent.keyboard('{Enter}')
    await expect(args.onInvoke).not.toHaveBeenCalled()
    await expect(canvas.getByText(/describe what you want/i)).toBeInTheDocument()
  },
}

/** AE-7: a typo is never a billable surprise. */
export const UnknownCommand: Story = {
  args: { draft: '/npcs a guard' },
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('textbox', { name: 'Message' }))
    await userEvent.keyboard('{Enter}')
    await expect(args.onInvoke).not.toHaveBeenCalled()
    await expect(canvas.getByText(/unknown tool/i)).toHaveTextContent('//')
  },
}

/** RAIL-3: the armed-tool row, and a Send button that says what it will run. */
export const Armed: Story = {
  args: { draft: '/monster CR 5, drowned' },
}

/** RAIL-13: no campaign selected. */
export const NoCampaign: Story = {
  args: { campaignSelected: false, onChooseCampaign: fn() },
}

/** AE-58: the capability lookup failed. */
export const CapabilityLookupFailed: Story = {
  args: { availability: toolAvailability(null), capabilitiesFailed: true, onRetryCapabilities: fn() },
}

/** RAIL-10: a disabled tool in the menu, reachable and refused. */
export const DisabledTools: Story = {
  args: { availability: DEFAULTS, draft: '/p' },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  args: { draft: '/npc the hooded stranger at the bar' },
}
