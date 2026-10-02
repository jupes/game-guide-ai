/**
 * NavRail -- the 56 px navigation rail (agent-forge-harness-1kg.6.3, T-11; LAYOUT-7).
 *
 * Measured in a real browser: its width, the 44 px floor of its controls, and a
 * keyboard focus ring in both themes. The rail is a landmark named Navigation rail.
 */
import * as React from 'react'
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { expectTouchTargets } from '../../.storybook/touchTarget'
import { expectTheme } from '../../.storybook/viewports'
import { NavRail } from './NavRail'

const inColumn: Decorator = (Story) => (
  <div style={{ display: 'flex', height: '320px', background: 'var(--md-sys-color-surface)' }}>
    <Story />
    {/* What the rail's aria-controls names: the shell's drawer host. */}
    <div id="drawer-host" />
  </div>
)

function Rail(props: Partial<React.ComponentProps<typeof NavRail>>): React.JSX.Element {
  const ref = React.useRef<HTMLButtonElement>(null)
  return <NavRail controls="drawer-host" expanded={false} onOpen={() => {}} openButtonRef={ref} {...props} />
}

const meta = {
  title: 'Shell/NavRail',
  component: NavRail,
  parameters: { layout: 'fullscreen' },
  decorators: [inColumn],
  render: (args) => <Rail {...args} />,
  args: { controls: 'drawer-host', expanded: false, onOpen: fn(), openButtonRef: { current: null } },
} satisfies Meta<typeof NavRail>

export default meta
type Story = StoryObj<typeof meta>

/** A player, or a GM outside a campaign: Open navigation and nothing else. */
export const OpenNavigationOnly: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const rail = canvas.getByRole('navigation', { name: 'Navigation rail' })
    await expect(rail.getBoundingClientRect().width).toBe(56)
    await expect(canvas.getAllByRole('button')).toHaveLength(1)
    await expectTouchTargets(canvas, ['Open navigation'])
  },
}

/** The Workbench is active: the Campaign Library button joins it. */
export const WithCampaignLibrary: Story = {
  args: { onToggleLibrary: fn(), libraryControls: 'library-panel' },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expectTouchTargets(canvas, ['Open navigation', 'Campaign Library'])
    const library = canvas.getByRole('button', { name: 'Campaign Library' })
    await expect(library).toHaveAttribute('title', 'Campaign Library')
    await expect(library).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(library)
    await expect(args.onToggleLibrary).toHaveBeenCalledTimes(1)
  },
}

/** The panel is open: the button says so. */
export const LibraryExpanded: Story = {
  // The decorator's drawer host stands in for the panel the button controls.
  args: { onToggleLibrary: fn(), libraryControls: 'drawer-host', libraryExpanded: true },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('button', { name: 'Campaign Library' })).toHaveAttribute('aria-expanded', 'true')
  },
}

/** The drawer is open: the button says so. */
export const Expanded: Story = {
  args: { expanded: true },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('button', { name: 'Open navigation' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  },
}

/** A keyboard user sees a focus ring on the rail's controls. */
export const FocusRing: Story = {
  args: { onToggleLibrary: fn() },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.tab()
    const open = canvas.getByRole('button', { name: 'Open navigation' })
    await expect(open).toHaveFocus()
    await expect(getComputedStyle(open).outlineStyle).toBe('solid')
    await expect(parseFloat(getComputedStyle(open).outlineWidth)).toBeGreaterThanOrEqual(3)
    await userEvent.tab()
    await expect(canvas.getByRole('button', { name: 'Campaign Library' })).toHaveFocus()
  },
}

export const FocusRingDark: Story = {
  ...FocusRing,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await FocusRing.play?.(context)
  },
}
