import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, within } from 'storybook/test'

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

/**
 * agent-forge-harness-27h — a selected filter chip is a pressed toggle, and it
 * says so now.
 *
 * Until this, the only evidence of which channel was open was a fill colour and
 * a check glyph marked `aria-hidden`, so a screen-reader user could switch
 * channels and never learn which one they were in. No axe rule covers a missing
 * selected state; the AppHeader and LeftNav shell stories are what found it. A
 * suggestion chip is a command rather than a toggle, so it takes no
 * `aria-pressed` at all.
 */
export const SelectedFilterIsPressed: Story = {
  render: () => (
    <div style={{ display: 'flex', gap: 8 }}>
      <Chip type="filter" icon="auto_stories" label="Sage" selected onClick={fn()} />
      <Chip type="filter" icon="gavel" label="Rules" onClick={fn()} />
      <Chip type="suggestion" icon="casino" label="Roll for it" onClick={fn()} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Sage' })).toHaveAttribute('aria-pressed', 'true')
    await expect(canvas.getByRole('button', { name: 'Rules' })).toHaveAttribute('aria-pressed', 'false')
    await expect(canvas.getByRole('button', { name: 'Roll for it' })).not.toHaveAttribute('aria-pressed')
  },
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

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1, and the sharpest case of the lot: Chip is
// one of the components THIS branch changed, and it had no dark story, so
// strict axe had never rendered a chip against the dark palette. The branch's
// own headline finding is a dark contrast failure that hid for exactly that
// reason (`--aether-nat20` at 4.49:1, invisible until a dark DiceRoll story
// existed). The dark stories below re-run the same play functions, so the
// `aria-pressed` contract and the dark colours are both covered.

export const Dark: Story = { ...SelectedFilterIsPressed, globals: { theme: 'dark' } }

export const DarkModeFilters: Story = { ...ModeFilters, globals: { theme: 'dark' } }

export const DarkInputWithRemove: Story = { ...InputWithRemove, globals: { theme: 'dark' } }
