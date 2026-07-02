import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

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
