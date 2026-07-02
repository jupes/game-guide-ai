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
