import type { Meta, StoryObj } from '@storybook/react-vite'

import { ChatMessage } from './ChatMessage'

const meta = {
  title: 'Aetheril/ChatMessage',
  component: ChatMessage,
  tags: ['autodocs'],
  argTypes: {
    role: {
      control: 'select',
      options: ['dm', 'player', 'system'],
    },
  },
  // Bubbles are designed for a chat column, not a centered point.
  parameters: { layout: 'padded' },
} satisfies Meta<typeof ChatMessage>

export default meta
type Story = StoryObj<typeof meta>

export const DungeonMaster: Story = {
  args: {
    role: 'dm',
    time: '8:42 PM',
    children:
      'As you push open the tavern door, warm candlelight spills onto the cobblestones. The barkeep looks up and nods toward an empty table near the hearth.',
  },
}

export const Player: Story = {
  args: {
    role: 'player',
    author: 'Astra Vail',
    time: '8:43 PM',
    children: 'I approach the barkeep and ask about the caravan job.',
  },
}

export const SystemNotice: Story = {
  args: {
    role: 'system',
    children: '✦ Astra rolled a Persuasion check: 17 (14 + 3)',
  },
}

export const Conversation: Story = {
  render: () => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 640 }}>
      <ChatMessage role="player" author="Astra Vail" time="8:41 PM">
        What does the wizard's tower look like up close?
      </ChatMessage>
      <ChatMessage role="dm" time="8:42 PM">
        Weathered obsidian, veined with silver runes that pulse faintly —
        like a heartbeat. The front door has no handle.
      </ChatMessage>
      <ChatMessage role="system">✦ Quest updated: Find another way in</ChatMessage>
      <ChatMessage role="player" author="Astra Vail" time="8:43 PM">
        I circle the tower looking for a servant's entrance.
      </ChatMessage>
    </div>
  ),
}
