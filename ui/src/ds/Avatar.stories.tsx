import type { Meta, StoryObj } from '@storybook/react-vite'

import { Avatar } from './Avatar'

// Self-contained portrait so stories make no network requests.
const PORTRAIT =
  "data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect width='80' height='80' fill='%237a5c2e'/%3E%3Ccircle cx='40' cy='30' r='14' fill='%23e8ddc8'/%3E%3Cellipse cx='40' cy='68' rx='24' ry='18' fill='%23e8ddc8'/%3E%3C/svg%3E"

const meta = {
  title: 'Aetheril/Avatar',
  component: Avatar,
  tags: ['autodocs'],
  argTypes: {
    tone: {
      control: 'select',
      options: ['gold', 'ember', 'verdigris', 'arcane'],
    },
  },
} satisfies Meta<typeof Avatar>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    name: 'Astra Vail',
    tone: 'gold',
    size: 40,
  },
}

export const Tones: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
      <Avatar name="Gold Tone" tone="gold" />
      <Avatar name="Ember Tone" tone="ember" />
      <Avatar name="Verdigris Tone" tone="verdigris" />
      <Avatar name="Arcane Tone" tone="arcane" />
    </div>
  ),
}

export const Sizes: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
      <Avatar name="Small Size" size={24} />
      <Avatar name="Default Size" size={40} />
      <Avatar name="Large Size" size={56} />
      <Avatar name="Extra Large" size={80} />
    </div>
  ),
}

export const DungeonMaster: Story = {
  args: {
    icon: 'auto_stories',
    tone: 'ember',
    ring: true,
  },
}

export const WithImage: Story = {
  args: {
    src: PORTRAIT,
    name: 'Astra Vail',
    size: 56,
  },
}

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1. This file had NO dark story, so strict axe
// had never rendered Avatar against the dark palette at all. That is exactly
// the hole that hid `--aether-nat20` at 4.49:1 on its dark container until a
// dark DiceRoll story was written for it: in a themed design system every
// colour is a DIFFERENT value per theme, so a light-only story is half a test.

export const Dark: Story = { ...Tones, globals: { theme: 'dark' } }

export const DarkWithImage: Story = { ...WithImage, globals: { theme: 'dark' } }
