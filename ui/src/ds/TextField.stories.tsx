import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

import { TextField } from './TextField'

const meta = {
  title: 'Aetheril/TextField',
  component: TextField,
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['filled', 'outlined'],
    },
  },
  args: { onChange: fn() },
} satisfies Meta<typeof TextField>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    label: 'Character name',
    placeholder: 'Astra Vail',
    variant: 'outlined',
  },
}

export const Variants: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
      <TextField variant="filled" label="Filled" placeholder="Ask…" />
      <TextField variant="outlined" label="Outlined" placeholder="Ask…" />
    </div>
  ),
}

export const WithIconsAndSupport: Story = {
  args: {
    label: 'Search the tomes',
    leadingIcon: 'search',
    trailingIcon: 'mic',
    supportingText: 'Searches all indexed rulebooks',
  },
}

export const ErrorState: Story = {
  args: {
    label: 'Spell slot level',
    value: 'ten',
    error: true,
    supportingText: 'Must be a number from 1 to 9',
  },
}

export const Multiline: Story = {
  args: {
    multiline: true,
    rows: 3,
    placeholder: 'Ask…',
    fullWidth: true,
  },
  parameters: { layout: 'padded' },
}

export const Disabled: Story = {
  args: {
    label: 'Sealed scroll',
    disabled: true,
  },
}
