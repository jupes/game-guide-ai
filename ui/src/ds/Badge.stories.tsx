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

/**
 * agent-forge-harness-27h — the same tone sweep in Dark Tavern.
 *
 * Not decoration. `--aether-nat20` on `--aether-nat20-container` read 4.14:1 in
 * light and 4.49:1 in dark, both under AA for this 11px bold label (bold only
 * counts as large text from 18.66px). Turning the axe gate on caught the light
 * one; the dark pair had no story at all, so axe had never rendered it. Both
 * themes are gated now.
 */
export const DarkTones: Story = {
  globals: { theme: 'dark' },
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
