import type { Meta, StoryObj } from '@storybook/react-vite'
import { fn } from 'storybook/test'

import { Card } from './Card'

const meta = {
  title: 'Aetheril/Card',
  component: Card,
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['elevated', 'filled', 'outlined'],
    },
  },
} satisfies Meta<typeof Card>

export default meta
type Story = StoryObj<typeof meta>

const sampleContent = (
  <>
    <h3 style={{ margin: '0 0 8px' }}>Quest Log</h3>
    <p style={{ margin: 0 }}>
      The party has agreed to escort the caravan through the Weathered Pass
      before the first snow.
    </p>
  </>
)

export const Playground: Story = {
  args: {
    variant: 'elevated',
    children: sampleContent,
  },
}

export const Variants: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', maxWidth: 900 }}>
      <Card variant="elevated" style={{ width: 240 }}>{sampleContent}</Card>
      <Card variant="filled" style={{ width: 240 }}>{sampleContent}</Card>
      <Card variant="outlined" style={{ width: 240 }}>{sampleContent}</Card>
    </div>
  ),
}

export const Interactive: Story = {
  args: {
    variant: 'elevated',
    interactive: true,
    onClick: fn(),
    children: sampleContent,
  },
}

export const Unpadded: Story = {
  args: {
    variant: 'outlined',
    padded: false,
    children: (
      <img
        alt="Map placeholder"
        src="data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='120'%3E%3Crect width='240' height='120' fill='%23d7c9a8'/%3E%3Ctext x='120' y='64' text-anchor='middle' font-family='Georgia' font-size='14' fill='%235a4a2a'%3EEdge-to-edge content%3C/text%3E%3C/svg%3E"
        style={{ display: 'block' }}
      />
    ),
  },
}
