/**
 * SourceList — the citations under an answer. Collapsed by default, with a
 * count that has to read correctly in the singular.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'

import { SourceList } from './SourceList'
import type { Source } from '../api'

function source(over: Partial<Source> = {}): Source {
  return {
    book: "Player's Handbook",
    chapter: 'Chapter 11: Spells',
    section: 'Shield',
    entity: 'Shield',
    page: 275,
    snippet:
      'An invisible barrier of magical force appears and protects you. Until the start of your next turn, you have a +5 bonus to AC.',
    ...over,
  }
}

const THREE: Source[] = [
  source(),
  source({
    book: "Dungeon Master's Guide",
    chapter: 'Chapter 5: Adventure Environments',
    section: 'Dungeon Hazards',
    entity: null,
    page: 105,
    snippet: 'Green slime devours flesh and organic material on contact and is even capable of eating through metal.',
  }),
  source({
    book: 'Monster Manual',
    chapter: null,
    section: null,
    entity: 'Owlbear',
    page: 249,
    snippet: 'An owlbear’s screech echoes through dark valleys and benighted forests, piercing the quiet night.',
  }),
]

const meta = {
  title: 'Shell/SourceList',
  component: SourceList,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: { sources: THREE },
} satisfies Meta<typeof SourceList>

export default meta
type Story = StoryObj<typeof meta>

export const Playground: Story = {}

/** The count is singular for one. Off-by-one plurals are the classic bug here. */
export const SingleSource: Story = {
  args: { sources: [source()] },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('1 source')).toBeInTheDocument()
  },
}

/** No sources at all — the component renders nothing rather than an empty shell. */
export const NoSources: Story = {
  args: { sources: [] },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector('details')).toBeNull()
  },
}

/**
 * The citations are reachable without a mouse: the summary takes focus, and it
 * is the whole disclosure control.
 *
 * Two things are deliberately NOT asserted from key presses here, and both are
 * limits of the runner rather than of the component. `userEvent.tab()` walks
 * its own focusable list, which does not include `<summary>`; and opening the
 * disclosure is the browser's native activation behaviour, which only runs for
 * TRUSTED events, while `userEvent` synthesises untrusted ones. (It
 * special-cases `<button>`, which is why the auth, model-picker and user-menu
 * stories really can press Enter and Space.) Forcing either would have proved
 * nothing. `Expanded` covers the opened state.
 */
export const SummaryTakesFocus: Story = {
  play: async ({ canvasElement }) => {
    const details = canvasElement.querySelector('details')
    const summary = canvasElement.querySelector('summary')
    await expect(details).not.toHaveAttribute('open')
    summary?.focus()
    await expect(summary).toHaveFocus()
  },
}

/** The opened state, with every citation and snippet on screen. */
export const Expanded: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByText('3 sources'))
    await expect(canvasElement.querySelector('details')).toHaveAttribute('open')
    await expect(canvas.getByText(/invisible barrier of magical force/)).toBeVisible()
  },
}

/** A retrieval chunk that ran long, and one with nothing but a book name. */
export const LongAndSparse: Story = {
  args: {
    sources: [
      source({
        snippet:
          'The spell’s description runs for most of a page, and the retriever returns the whole chunk. '.repeat(
            8,
          ),
      }),
      source({ book: 'Xanathar’s Guide to Everything', chapter: null, section: null, entity: null, page: null, snippet: 'A short note.' }),
    ],
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByText('2 sources'))
    // With no entity, section or chapter the book name IS the heading — so it
    // appears twice, once bolded as the heading and once as the book line.
    await expect(canvas.getAllByText('Xanathar’s Guide to Everything')).toHaveLength(2)
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkExpanded: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByText('3 sources'))
    await expect(canvas.getByText(/screech echoes through dark valleys/)).toBeVisible()
  },
}
