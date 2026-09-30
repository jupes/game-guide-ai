import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect } from 'storybook/test'

import { StatBlockCard } from './StatBlockCard'

const meta = {
  title: 'Aetheril/StatBlockCard',
  component: StatBlockCard,
  tags: ['autodocs'],
  argTypes: {
    density: {
      control: 'select',
      options: ['default', 'compact'],
    },
  },
} satisfies Meta<typeof StatBlockCard>

export default meta
type Story = StoryObj<typeof meta>

export const GoblinScout: Story = {
  args: {
    name: 'Goblin Scout',
    size: 'Small',
    type: 'humanoid',
    alignment: 'neutral evil',
    ac: 15,
    hp: 7,
    hitDice: '2d6',
    speed: '30 ft.',
    abilities: { str: 8, dex: 14, con: 10, int: 10, wis: 8, cha: 8 },
    savingThrows: 'Dex +4',
    skills: 'Stealth +6',
    damageImmunities: 'poison',
    conditionImmunities: 'poisoned',
    senses: 'darkvision 60 ft., passive Perception 9',
    languages: 'Common, Goblin',
    cr: '1/4',
    xp: 50,
    traits: [
      {
        name: 'Nimble Escape',
        text: 'The goblin can take the Disengage or Hide action as a bonus action on each of its turns.',
      },
    ],
    actions: [
      { name: 'Scimitar', text: 'Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.' },
    ],
    source: 'mm-2024',
  },
}

export const Compact: Story = {
  args: {
    ...GoblinScout.args,
    density: 'compact',
  },
}

export const MinimalCoreOnly: Story = {
  args: {
    name: 'Mystery Creature',
    ac: 10,
    hp: 4,
  },
}

// ── Dark Tavern ──────────────────────────────────────────────────────────────
// agent-forge-harness-27h, rework 1. This file had NO dark story, so strict axe
// had never rendered StatBlockCard against the dark palette at all. That is exactly
// the hole that hid `--aether-nat20` at 4.49:1 on its dark container until a
// dark DiceRoll story was written for it: in a themed design system every
// colour is a DIFFERENT value per theme, so a light-only story is half a test.

export const Dark: Story = { ...GoblinScout, globals: { theme: 'dark' } }

/**
 * agent-forge-harness-0rn (INF-15): the width ChatPane gives a card at a 320px
 * viewport. The details grid's 250px track minimum used to overflow the
 * details grid's content box, so every detail row ran 24px into the padding
 * (and under the card's edge at 320px), with no page-level overflow to show it.
 */
export const NarrowColumn: Story = {
  args: GoblinScout.args,
  decorators: [
    (Story) => (
      <div style={{ width: 276 }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const details = canvasElement.querySelector('.stat-block-card__details')
    if (!(details instanceof HTMLElement)) throw new Error('no details grid')
    // Each detail row ends inside the grid's content box. `scrollWidth` alone
    // cannot see this: a row that runs 24px into the end padding stays inside
    // the element's own box.
    const style = getComputedStyle(details)
    const contentRight =
      details.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth)
    const rows = Array.from(details.children)
    await expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      await expect(row.getBoundingClientRect().right).toBeLessThanOrEqual(contentRight + 1)
    }
  },
}
