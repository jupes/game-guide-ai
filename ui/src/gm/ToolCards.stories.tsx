import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'
import { ToolCardBody } from './ToolCards'
import { cardResult, toolInvocation, TOOL_CARD_FIXTURES } from './laneFixtures'
import { CARD_TITLE_MAX_CHARS, HOOK_TEXT_MAX_CHARS, HOOKS_MAX_ENTRIES, LOOT_MAX_ITEMS, LOOT_NOTE_MAX_CHARS,
  NAME_MAX_CHARS, NAMES_MAX_ENTRIES, RULES_ANSWER_MAX_CHARS } from './contracts'

const meta = {
  title: 'Aetheril/Workbench/Tool cards',
  component: ToolCardBody,
  args: { card: TOOL_CARD_FIXTURES.loot },
  parameters: { layout: 'padded' },
} satisfies Meta<typeof ToolCardBody>

export default meta
type Story = StoryObj<typeof meta>

const monster = toolInvocation({ tool_id: 'monster', status: 'done', result: cardResult() }).result

const gallery: Story = {
  render: () => (
    <div>
      {monster?.result_kind === 'card' && <ToolCardBody card={monster.card} />}
      {Object.values(TOOL_CARD_FIXTURES).map((card) => <ToolCardBody key={card.card_kind} card={card} />)}
    </div>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    for (const title of ['Drowned Thing', 'The offering box', 'Names for the coast', 'Riverside hooks', 'Using cover']) {
      await expect(canvas.getByRole('heading', { name: title, level: 3 })).toBeVisible()
    }
  },
}

export const Light: Story = { ...gallery, globals: { theme: 'light' } }
export const Dark: Story = { ...gallery, globals: { theme: 'dark' } }

const unbroken = (length: number) => 'x'.repeat(length)
const literal = '![image](https://example.test/image)'

export const LongTextOnPhone: Story = {
  globals: { theme: 'dark' },
  render: () => (
    <div style={{ width: '100%', maxWidth: 375 }} data-testid="narrow-tool-card-gallery">
      <ToolCardBody card={{ card_kind: 'loot', loot: { title: unbroken(CARD_TITLE_MAX_CHARS), items: Array.from(
        { length: LOOT_MAX_ITEMS }, () => ({ name: literal, note: unbroken(LOOT_NOTE_MAX_CHARS) }),
      ) } }} />
      <ToolCardBody card={{ card_kind: 'names', names: { title: 'Long names', entries: Array.from(
        { length: NAMES_MAX_ENTRIES }, () => ({ name: unbroken(NAME_MAX_CHARS), note: literal }),
      ) } }} />
      <ToolCardBody card={{ card_kind: 'hooks', hooks: { title: 'Long hooks', entries: Array.from(
        { length: HOOKS_MAX_ENTRIES }, () => ({ title: literal, text: unbroken(HOOK_TEXT_MAX_CHARS) }),
      ) } }} />
      <ToolCardBody card={{ card_kind: 'rules', rules: { ...TOOL_CARD_FIXTURES.rules.rules, answer: unbroken(RULES_ANSWER_MAX_CHARS) } }} />
    </div>
  ),
  play: async ({ canvasElement }) => {
    const gallery = within(canvasElement).getByTestId('narrow-tool-card-gallery')
    await expect(gallery.scrollWidth).toBeLessThanOrEqual(gallery.clientWidth)
    await expect(gallery.querySelector('img, a, script, iframe')).toBeNull()
  },
}
