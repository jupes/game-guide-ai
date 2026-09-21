import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

import { IconButton } from './IconButton'

const meta = {
  title: 'Aetheril/IconButton',
  component: IconButton,
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['standard', 'filled', 'tonal', 'outlined'],
    },
    size: {
      control: 'select',
      options: ['small', 'medium', 'large'],
    },
  },
  // icon is a required prop — a meta-level default keeps render()-only stories typed.
  args: { onClick: fn(), icon: 'casino', ariaLabel: 'Icon button' },
} satisfies Meta<typeof IconButton>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    icon: 'casino',
    ariaLabel: 'Roll dice',
    variant: 'standard',
    size: 'medium',
  },
}

export const Variants: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
      <IconButton icon="send" ariaLabel="Send (standard)" variant="standard" />
      <IconButton icon="send" ariaLabel="Send (filled)" variant="filled" />
      <IconButton icon="send" ariaLabel="Send (tonal)" variant="tonal" />
      <IconButton icon="send" ariaLabel="Send (outlined)" variant="outlined" />
    </div>
  ),
}

export const Selected: Story = {
  args: {
    icon: 'bookmark',
    ariaLabel: 'Bookmarked',
    selected: true,
  },
}

export const Disabled: Story = {
  args: {
    icon: 'download',
    ariaLabel: 'Export chat',
    disabled: true,
  },
}

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1. This file had NO dark story, so strict axe
// had never rendered IconButton against the dark palette at all. That is exactly
// the hole that hid `--aether-nat20` at 4.49:1 on its dark container until a
// dark DiceRoll story was written for it: in a themed design system every
// colour is a DIFFERENT value per theme, so a light-only story is half a test.

export const Dark: Story = { ...Variants, globals: { theme: 'dark' } }

export const DarkSelected: Story = { ...Selected, globals: { theme: 'dark' } }
