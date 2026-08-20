import type { Meta, StoryObj } from '@storybook/react-vite'

import { GameContentCard, GameContentSection, GameContentEntry } from './GameContentCard'

const meta = {
  title: 'Aetheril/GameContentCard',
  component: GameContentCard,
  tags: ['autodocs'],
  argTypes: {
    density: {
      control: 'select',
      options: ['default', 'compact'],
    },
  },
} satisfies Meta<typeof GameContentCard>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {
  args: {
    kind: 'spell',
    title: 'Fireball',
    qualifier: '3rd-level evocation',
    stats: [
      { label: 'Casting Time', value: '1 action' },
      { label: 'Range', value: '150 feet' },
    ],
    source: 'phb-2024',
  },
  render: (args) => (
    <GameContentCard {...args}>
      <GameContentSection first>
        <p style={{ margin: 0 }}>
          A bright streak flashes from you to a point you choose within range and then blossoms
          with a low roar into an explosion of flame.
        </p>
      </GameContentSection>
    </GameContentCard>
  ),
}

export const WithEntries: Story = {
  args: {
    kind: 'statblock',
    title: 'Goblin Scout',
    qualifier: 'Small humanoid, neutral evil',
  },
  render: (args) => (
    <GameContentCard {...args}>
      <GameContentSection title="Traits" first>
        <GameContentEntry name="Nimble Escape">
          The goblin can take the Disengage or Hide action as a bonus action.
        </GameContentEntry>
      </GameContentSection>
      <GameContentSection title="Actions">
        <GameContentEntry name="Scimitar">Melee Weapon Attack: +4 to hit.</GameContentEntry>
      </GameContentSection>
    </GameContentCard>
  ),
}

export const Compact: Story = {
  args: {
    ...Playground.args,
    density: 'compact',
  },
  render: Playground.render,
}
