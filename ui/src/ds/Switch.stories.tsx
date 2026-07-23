import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, within } from 'storybook/test'

import { Switch } from './Switch'
import type { SwitchProps } from './Switch'

const meta = {
  title: 'Aetheril/Switch',
  component: Switch,
  tags: ['autodocs'],
  args: { onChange: fn() },
} satisfies Meta<typeof Switch>

export default meta
type Story = StoryObj<typeof meta>

/** Stateful wrapper so the toggle actually flips in the canvas. */
function ControlledSwitch(props: SwitchProps) {
  const [checked, setChecked] = React.useState(props.checked ?? false)
  return (
    <Switch
      {...props}
      checked={checked}
      onChange={(next) => {
        setChecked(next)
        props.onChange?.(next)
      }}
    />
  )
}

export const Playground: Story = {
  args: {
    ariaLabel: 'Dark theme',
    checked: false,
  },
  render: (args) => <ControlledSwitch {...args} />,
}

export const Checked: Story = {
  args: {
    ariaLabel: 'Dark theme',
    checked: true,
  },
}

export const WithIcons: Story = {
  args: {
    ariaLabel: 'Dark theme',
    icons: true,
    checked: true,
  },
  render: (args) => <ControlledSwitch {...args} />,
}

export const Disabled: Story = {
  args: {
    ariaLabel: 'Unavailable option',
    disabled: true,
  },
}

export const VisualStates: Story = {
  render: (args) => (
    <div style={{ display: 'flex', gap: '24px' }}>
      <Switch {...args} ariaLabel="Off switch" checked={false} />
      <Switch {...args} ariaLabel="On switch" checked />
    </div>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const offControl = canvas.getByRole('switch', { name: 'Off switch' })
    const onControl = canvas.getByRole('switch', { name: 'On switch' })
    const offTrack = offControl.querySelector<HTMLElement>('.aether-switch')!
    const onTrack = onControl.querySelector<HTMLElement>('.aether-switch')!
    const offHandle = offControl.querySelector<HTMLElement>('.aether-switch__handle')!
    const onHandle = onControl.querySelector<HTMLElement>('.aether-switch__handle')!

    await expect(getComputedStyle(offTrack).backgroundColor).not.toBe(
      getComputedStyle(onTrack).backgroundColor,
    )
    await expect(getComputedStyle(offHandle).left).not.toBe(
      getComputedStyle(onHandle).left,
    )

    offControl.focus()
    await expect(getComputedStyle(offTrack).outlineStyle).not.toBe('none')
  },
}

export const DarkVisualStates: Story = {
  ...VisualStates,
  globals: { theme: 'dark' },
}
