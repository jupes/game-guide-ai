import type { Meta, StoryObj } from '@storybook/react-vite'

import { DiceRoll } from './DiceRoll'

const meta = {
  title: 'Aetheril/DiceRoll',
  component: DiceRoll,
  tags: ['autodocs'],
  argTypes: {
    size: {
      control: 'select',
      options: ['sm', 'md', 'lg'],
    },
  },
} satisfies Meta<typeof DiceRoll>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    die: 20,
    value: 14,
    modifier: 3,
    label: 'Stealth check',
  },
}

export const Nat20: Story = {
  args: {
    die: 20,
    value: 20,
    label: 'Attack roll',
  },
}

export const Nat1: Story = {
  args: {
    die: 20,
    value: 1,
    label: 'Saving throw',
  },
}

export const NegativeModifier: Story = {
  args: {
    die: 20,
    value: 12,
    modifier: -2,
    label: 'Strength check',
  },
}

export const Rolling: Story = {
  args: {
    die: 20,
    rolling: true,
    label: 'Rolling…',
  },
}

export const Sizes: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
      <DiceRoll die={20} value={11} size="sm" label="sm" />
      <DiceRoll die={20} value={11} size="md" label="md" />
      <DiceRoll die={20} value={11} size="lg" label="lg" />
    </div>
  ),
}

/**
 * agent-forge-harness-27h — the crit tones in Dark Tavern.
 *
 * The pip value is 20px normal text on the tone's container. The nat20 pair was
 * 4.14:1 light (which the axe gate caught the moment it was turned on) and
 * 4.49:1 dark — equally under AA, and invisible only because nothing rendered
 * the dark theme. Both are gated here now.
 */
export const DarkCrits: Story = {
  globals: { theme: 'dark' },
  render: () => (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
      <DiceRoll die={20} value={20} label="Attack roll" />
      <DiceRoll die={20} value={1} label="Saving throw" />
      <DiceRoll die={20} value={14} modifier={3} label="Stealth check" />
    </div>
  ),
}
