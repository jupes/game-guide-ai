/**
 * SelectionBar — CANVAS-23's targeted edit: three complete requests over a
 * selection inside one field.
 *
 * It is a `role="toolbar"`, and that is a promise to a keyboard user: ONE tab
 * stop on the way in, arrow keys between the actions. These stories keep the
 * promise honest — a toolbar with three tab stops and no arrow keys would look
 * identical in a screenshot and pass every axe rule.
 *
 * The bar is absolutely positioned from a computed placement, so the stories
 * put it inside a relatively-positioned box exactly as a document pane does.
 */

import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { SelectionBar } from './SelectionBar'
import type { DocumentSelection } from './SelectionBar'
import { selectionBarPosition } from './documentFields'

const SELECTION: DocumentSelection = {
  field: 'if_attacked',
  label: 'If the party attacks',
  start: 0,
  end: 62,
  text: 'She does not fight. She puts the counter between herself and the door.',
}

/** The real geometry function, not a hand-written pair of coordinates. */
const CONTAINER = { left: 0, top: 0, width: 520, height: 260 }
const BAR = { width: 240, height: 40 }
const ABOVE = selectionBarPosition(
  { left: 60, top: 120, width: 300, height: 22 },
  CONTAINER,
  BAR,
)
const BELOW = selectionBarPosition(
  { left: 60, top: 4, width: 300, height: 22 },
  CONTAINER,
  BAR,
)

const meta = {
  title: 'GM/SelectionBar',
  component: SelectionBar,
  tags: ['autodocs'],
  parameters: { layout: 'padded' },
  args: {
    selection: SELECTION,
    placement: ABOVE,
    onAction: fn(),
    onDismiss: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ position: 'relative', width: CONTAINER.width, height: CONTAINER.height }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof SelectionBar>

export default meta
type Story = StoryObj<typeof meta>

/** The bar's preferred placement: above the text it acts on, never over it. */
export const AboveTheSelection: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const toolbar = canvas.getByRole('toolbar', {
      name: 'Ask the assistant about the selected text in If the party attacks',
    })
    await expect(toolbar).toHaveAttribute('data-placement', 'above')
    await expect(canvas.getAllByRole('button')).toHaveLength(3)
  },
}

/** A selection at the very top of the pane: the bar drops below instead. */
export const BelowTheSelection: Story = {
  args: { placement: BELOW },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('toolbar')).toHaveAttribute('data-placement', 'below')
  },
}

/**
 * The toolbar contract, by keyboard alone: one Tab reaches the bar, the arrows
 * move between the actions and WRAP, Home and End jump to the ends, and Tab
 * leaves — so the wrap can never become a trap.
 */
export const OneTabStopAndArrowKeys: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const [rewrite, shorter, darker] = canvas.getAllByRole('button')

    await userEvent.tab()
    await expect(rewrite).toHaveFocus()

    await userEvent.keyboard('{ArrowRight}')
    await expect(shorter).toHaveFocus()
    await userEvent.keyboard('{ArrowRight}')
    await expect(darker).toHaveFocus()

    // Wraps forwards…
    await userEvent.keyboard('{ArrowRight}')
    await expect(rewrite).toHaveFocus()
    // …and backwards.
    await userEvent.keyboard('{ArrowLeft}')
    await expect(darker).toHaveFocus()

    await userEvent.keyboard('{Home}')
    await expect(rewrite).toHaveFocus()
    await userEvent.keyboard('{End}')
    await expect(darker).toHaveFocus()

    // Tab is the way out: the bar is one stop, not three.
    await userEvent.tab()
    await expect(darker).not.toHaveFocus()
  },
}

/** Each action reports itself with the selection it was invoked on. */
export const ActionRunsOnTheSelection: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Shorter' }).focus()
    await userEvent.keyboard('{Enter}')
    await expect(args.onAction).toHaveBeenCalledWith('shorter', SELECTION)
  },
}

/** Escape dismisses. The OWNER returns focus — a control that is unmounting
 * cannot decide where focus lands, which is why the bar only reports. */
export const DismissedByEscape: Story = {
  play: async ({ args, canvasElement }) => {
    const canvas = within(canvasElement)
    canvas.getByRole('button', { name: 'Rewrite' }).focus()
    await userEvent.keyboard('{Escape}')
    await expect(args.onDismiss).toHaveBeenCalled()
  },
}

/** A long field label: the accessible name carries it in full. */
export const LongFieldLabel: Story = {
  args: {
    selection: {
      ...SELECTION,
      label: 'What the innkeeper does if the party attacks her in her own common room',
    },
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(
      canvas.getByRole('toolbar', { name: /in her own common room$/ }),
    ).toBeInTheDocument()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
}

export const DarkBelow: Story = {
  globals: { theme: 'dark' },
  args: { placement: BELOW },
}
