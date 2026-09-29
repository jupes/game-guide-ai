/**
 * Canaries for `expectNothingClipped` (`.storybook/viewports.ts`,
 * agent-forge-harness-9m2).
 *
 * A node that clips on purpose (an ellipsised line, an intended scroller)
 * exempts what is INSIDE it, not its own box. Before 9m2 the walk skipped
 * such a node before measuring it, so a title or a channel strip pushed out
 * of its clipping box passed every phone story. Nothing in the product
 * stories fails if that skip comes back, so these do: each renders the same
 * fixture twice, in place (its contents overflow it and are exempt, so the
 * walk passes) and pushed 150px right inside a 200px `overflow: hidden` box
 * (its own box crosses, so the walk must name it and the assertion reject).
 */
import type { Meta, StoryObj } from '@storybook/react-vite'
import type { CSSProperties, ReactNode } from 'react'
import { expect, within } from 'storybook/test'

import { clippedNodes, expectNothingClipped } from '../../.storybook/viewports'

const meta = {
  title: 'Testing/expectNothingClipped',
  parameters: { layout: 'padded' },
} satisfies Meta

export default meta
type Story = StoryObj<typeof meta>

/** The clipping box: 200px wide, hides what passes its edge. */
const BOX: CSSProperties = { width: 200, overflow: 'hidden', marginBottom: 16 }

/** Moves a 120px node from x=0 to x=150 inside BOX: 70px past its right edge. */
const PUSHED: CSSProperties = { position: 'relative', left: 150 }

function Box({ testId, children }: { testId: string; children: ReactNode }) {
  return (
    <div className="clip-canary__box" data-testid={testId} style={BOX}>
      {children}
    </div>
  )
}

function Title({ style }: { style?: CSSProperties }) {
  return (
    <span
      className="clip-canary__title"
      style={{
        display: 'block',
        width: 120,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      The Weathered Pass, before the first snow
    </span>
  )
}

/** `.app-header__channels` is one of INTENDED_SCROLLERS; the inline styles
 * make it a scroller here without the product stylesheet. Its buttons keep
 * the scrollable region reachable from the keyboard, as the real strip's do. */
function Scroller({ label, style }: { label: string; style?: CSSProperties }) {
  return (
    <div
      className="app-header__channels"
      role="group"
      aria-label={label}
      style={{ display: 'flex', gap: 8, width: 120, overflowX: 'auto', ...style }}
    >
      {['Rules', 'Lore', 'Spells', 'Monsters', 'Items'].map((name) => (
        <button key={name} type="button" style={{ flex: 'none', width: 72 }}>
          {name}
        </button>
      ))}
    </div>
  )
}

/** The in-place box passes; the pushed one yields exactly `crossing`, and
 * expectNothingClipped itself rejects on it. */
async function expectOwnBoxMeasured(canvasElement: HTMLElement, crossing: string): Promise<void> {
  const canvas = within(canvasElement)
  const inPlace = canvas.getByTestId('in-place')
  const pushed = canvas.getByTestId('pushed')
  await expect(await clippedNodes(inPlace)).toEqual([])
  await expectNothingClipped(inPlace)
  await expect(await clippedNodes(pushed)).toEqual([crossing])
  await expect(expectNothingClipped(pushed)).rejects.toThrow()
}

export const EllipsisOwnBoxIsMeasured: Story = {
  render: () => (
    <>
      <Box testId="in-place">
        <Title />
      </Box>
      <Box testId="pushed">
        <Title style={PUSHED} />
      </Box>
    </>
  ),
  // The title's text overflows the title (that is the ellipsis) and is exempt.
  play: async ({ canvasElement }) =>
    expectOwnBoxMeasured(canvasElement, 'span.clip-canary__title crosses div.clip-canary__box by 70px right'),
}

export const ScrollerOwnBoxIsMeasured: Story = {
  render: () => (
    <>
      <Box testId="in-place">
        <Scroller label="Channels in place" />
      </Box>
      <Box testId="pushed">
        <Scroller label="Channels pushed out" style={PUSHED} />
      </Box>
    </>
  ),
  // The strip's buttons run past the strip (they scroll) and are exempt.
  play: async ({ canvasElement }) =>
    expectOwnBoxMeasured(
      canvasElement,
      'div.app-header__channels "Channels pushed out" crosses div.clip-canary__box by 70px right',
    ),
}
