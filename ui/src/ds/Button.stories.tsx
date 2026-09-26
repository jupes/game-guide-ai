import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

import { Button } from './Button'

const meta = {
  title: 'Aetheril/Button',
  component: Button,
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['filled', 'tonal', 'elevated', 'outlined', 'text'],
    },
    size: {
      control: 'select',
      options: ['small', 'medium', 'large'],
    },
  },
  args: { onClick: fn() },
} satisfies Meta<typeof Button>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    children: 'Enter the Tavern',
    variant: 'filled',
    size: 'medium',
    icon: 'login',
  },
}

export const Variants: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
      <Button variant="filled">Filled</Button>
      <Button variant="tonal">Tonal</Button>
      <Button variant="elevated">Elevated</Button>
      <Button variant="outlined">Outlined</Button>
      <Button variant="text">Text</Button>
    </div>
  ),
}

export const Sizes: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
      <Button size="small">Small</Button>
      <Button size="medium">Medium</Button>
      <Button size="large">Large</Button>
    </div>
  ),
}

export const WithIcons: Story = {
  args: {
    children: 'Roll Initiative',
    icon: 'casino',
    trailingIcon: 'arrow_forward',
  },
}

export const Disabled: Story = {
  args: {
    children: 'Unavailable',
    disabled: true,
  },
}

export const FullWidth: Story = {
  parameters: { layout: 'padded' },
  args: {
    children: 'Begin the Campaign',
    fullWidth: true,
  },
}

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1. This file had NO dark story, so strict axe
// had never rendered Button against the dark palette at all. That is exactly
// the hole that hid `--aether-nat20` at 4.49:1 on its dark container until a
// dark DiceRoll story was written for it: in a themed design system every
// colour is a DIFFERENT value per theme, so a light-only story is half a test.

export const Dark: Story = { ...Variants, globals: { theme: 'dark' } }
