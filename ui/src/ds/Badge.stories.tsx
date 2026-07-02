import type { Meta, StoryObj } from '@storybook/react-vite'

import { Badge } from './Badge'

const meta = {
  title: 'Aetheril/Badge',
  component: Badge,
  tags: ['autodocs'],
  argTypes: {
    tone: {
      control: 'select',
      options: ['primary', 'neutral', 'gold', 'verdigris', 'arcane', 'error', 'nat20', 'nat1'],
    },
  },
} satisfies Meta<typeof Badge>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    children: '3',
    tone: 'primary',
  },
}

export const Tones: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
      <Badge tone="primary">primary</Badge>
      <Badge tone="neutral">neutral</Badge>
      <Badge tone="gold">gold</Badge>
      <Badge tone="verdigris">verdigris</Badge>
      <Badge tone="arcane">arcane</Badge>
      <Badge tone="error">error</Badge>
      <Badge tone="nat20">NAT 20</Badge>
      <Badge tone="nat1">NAT 1</Badge>
    </div>
  ),
}

export const Dot: Story = {
  args: {
    dot: true,
    tone: 'error',
  },
}
