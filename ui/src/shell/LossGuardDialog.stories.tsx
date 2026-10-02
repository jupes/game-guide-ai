/**
 * LossGuardDialog -- the unsaved-changes dialog (agent-forge-harness-1kg.6.3, T-7;
 * §5.3, CANVAS-16, AE-47). Keyboard-first: Keep editing takes focus, Tab wraps, Escape
 * keeps editing, and the focus ring shows in both themes.
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { LossGuardDialog } from './LossGuardDialog'

const meta = {
  title: 'Shell/LossGuardDialog',
  component: LossGuardDialog,
  parameters: { layout: 'fullscreen' },
  args: {
    title: 'Sister Ondrey Vashe',
    retryable: true,
    busy: false,
    onKeep: fn(),
    onRetry: fn(),
    onDiscard: fn(),
  },
} satisfies Meta<typeof LossGuardDialog>

export default meta
type Story = StoryObj<typeof meta>

/** The flush failed but a retry may work: all three choices. Keep editing is focused. */
export const Retryable: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const dialog = canvas.getByRole('dialog', { name: 'You have unsaved changes to Sister Ondrey Vashe' })
    await expect(dialog).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Keep editing' })).toHaveFocus()
    await expectTouchTargets(canvas, ['Keep editing', 'Try saving again', 'Discard changes'])
  },
}

/** Nothing a retry could fix: Try saving again is not offered. */
export const NotRetryable: Story = {
  args: { retryable: false },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'Try saving again' })).toBeNull()
    await expect(canvas.getByRole('button', { name: 'Discard changes' })).toBeVisible()
  },
}

/** A retry is running: it cannot be pressed again, and nothing can be discarded meanwhile. */
export const Busy: Story = {
  args: { busy: true },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Saving…' })).toBeDisabled()
    await expect(canvas.getByRole('button', { name: 'Discard changes' })).toBeDisabled()
  },
}

/** Tab wraps in both directions, and Escape keeps editing: nothing is discarded. */
export const KeyboardTrapsAndKeeps: Story = {
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    const keep = canvas.getByRole('button', { name: 'Keep editing' })
    const discard = canvas.getByRole('button', { name: 'Discard changes' })
    await expect(keep).toHaveFocus()
    await userEvent.tab({ shift: true })
    await expect(discard).toHaveFocus()
    await userEvent.tab()
    await expect(keep).toHaveFocus()
    await expect(getComputedStyle(keep).outlineStyle).toBe('solid')
    await userEvent.keyboard('{Escape}')
    await expect(args.onKeep).toHaveBeenCalledTimes(1)
    await expect(args.onDiscard).not.toHaveBeenCalled()
  },
}

export const Dark: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expect(within(canvasElement).getByRole('button', { name: 'Keep editing' })).toHaveFocus()
  },
}

/** Narrow: a full-width bottom sheet (keyed on the shell's `data-nav`, so it needs the shell root to say narrow). */
export const PhoneSheet: Story = {
  ...atViewport('phone375'),
  decorators: [
    (Story) => (
      <div className="workspace-shell" data-layout="narrow" data-nav="hidden">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = within(canvasElement)
    const dialog = canvas.getByRole('dialog')
    const box = dialog.getBoundingClientRect()
    await expect(box.width).toBe(375)
    await expect(Math.abs(box.bottom - window.innerHeight)).toBeLessThanOrEqual(1)
    await expectNoPageOverflow()
  },
}
