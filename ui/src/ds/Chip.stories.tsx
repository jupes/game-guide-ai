import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

import { Chip } from './Chip'

const meta = {
  title: 'Aetheril/Chip',
  component: Chip,
  tags: ['autodocs'],
  argTypes: {
    type: {
      control: 'select',
      options: ['assist', 'filter', 'input', 'suggestion'],
    },
  },
  // Default for gallery stories that use render() — label is a required prop.
  args: { label: 'Chip' },
} satisfies Meta<typeof Chip>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    label: 'Sage',
    type: 'filter',
    icon: 'auto_stories',
    onClick: fn(),
  },
}

export const ModeFilters: Story = {
  render: () => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <Chip type="filter" icon="auto_stories" label="Sage" selected onClick={fn()} />
      <Chip type="filter" icon="auto_awesome" label="Spell" onClick={fn()} />
      <Chip type="filter" icon="gavel" label="Rules" onClick={fn()} />
      <Chip type="filter" icon="castle" label="GM" onClick={fn()} />
    </div>
  ),
}

export const Suggestion: Story = {
  args: {
    label: 'Ask the Sage',
    type: 'suggestion',
    icon: 'auto_stories',
    onClick: fn(),
  },
}

export const InputWithRemove: Story = {
  args: {
    label: "Player's Handbook",
    type: 'input',
    onRemove: fn(),
  },
}

export const Disabled: Story = {
  args: {
    label: 'Locked',
    type: 'assist',
    icon: 'lock',
    disabled: true,
  },
}
