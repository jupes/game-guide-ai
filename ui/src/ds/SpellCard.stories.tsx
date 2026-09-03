import type { Meta, StoryObj } from '@storybook/react-vite'

import { SpellCard } from './SpellCard'

const meta = {
  title: 'Aetheril/SpellCard',
  component: SpellCard,
  tags: ['autodocs'],
  argTypes: {
    density: {
      control: 'select',
      options: ['default', 'compact'],
    },
  },
} satisfies Meta<typeof SpellCard>

export default meta
type Story = StoryObj<typeof meta>

export const Fireball: Story = {
  args: {
    name: 'Fireball',
    level: 3,
    school: 'evocation',
    castingTime: '1 action',
    range: '150 feet',
    duration: 'Instantaneous',
    components: { v: true, s: true, m: 'a tiny ball of bat guano and sulfur' },
    description:
      'A bright streak flashes from you to a point you choose within range and then blossoms '
      + 'with a low roar into an explosion of flame.',
    higherLevels: 'the damage increases by 1d6 for each slot level above 3rd',
    classes: ['Sorcerer', 'Wizard'],
    source: 'phb-2024',
  },
}

export const Cantrip: Story = {
  args: {
    name: 'Fire Bolt',
    level: 0,
    school: 'evocation',
    castingTime: '1 action',
    range: '120 feet',
    duration: 'Instantaneous',
    components: { v: true, s: true },
    description: 'You hurl a mote of fire at a creature or object within range.',
  },
}

export const ConcentrationRitual: Story = {
  args: {
    name: 'Detect Magic',
    level: 1,
    school: 'divination',
    castingTime: '1 action',
    range: 'Self',
    duration: 'Concentration, up to 10 minutes',
    components: { v: true, s: true },
    description: 'You sense the presence of magic within 30 feet of you.',
    concentration: true,
    ritual: true,
  },
}

export const Compact: Story = {
  args: {
    ...Fireball.args,
    density: 'compact',
  },
}
