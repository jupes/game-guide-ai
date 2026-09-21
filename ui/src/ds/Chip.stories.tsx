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
