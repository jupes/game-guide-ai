import type { Meta, StoryObj } from '@storybook/react-vite'

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
