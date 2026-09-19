import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, waitFor, within } from 'storybook/test'

import { CanvasPane } from './CanvasPane'
import type { DocumentVersion } from './contracts'

function version(overrides: Partial<DocumentVersion> & { number: number }): DocumentVersion {
  return {
    author: 'assistant',
    summary: `Change ${overrides.number}`,
    created_at: '2026-09-16T19:36:00Z',
    sealed: true,
    changed_fields: [],
    restored_from: null,
    ...overrides,
  }
}

const VERSIONS: DocumentVersion[] = [
  version({ number: 3, summary: 'Wants the signet', changed_fields: ['wants'] }),
  version({
    number: 2,
    author: 'gm',
    summary: 'Voice, by hand',
    created_at: '2026-09-16T19:34:00Z',
    changed_fields: ['voice'],
  }),
  version({ number: 1, summary: 'First draft', created_at: '2026-09-16T19:31:00Z' }),
]

/** 1kg.6.2 renders the real document; the pane only holds it. */
function Dossier(): React.JSX.Element {
  return (
    <div style={{ display: 'grid', gap: 16, maxWidth: '38rem' }}>
      <p style={{ margin: 0 }}>
        Quiet, clipped. A silver pin at the collar she never takes off, and a wariness she has stopped
        apologising for.
      </p>
      <p style={{ margin: 0 }}>
        She wants the family signet — proof the drowning of Vault Harbour was ordered by name, not
        swallowed by accident.
      </p>
    </div>
  )
}

const meta = {
  title: 'GM Workbench/CanvasPane',
  component: CanvasPane,
  parameters: { layout: 'fullscreen' },
  args: {
    title: 'Sister Ondrey Vashe',
    documentType: 'npc',
    updatedAt: '2026-09-16T19:36:00Z',
    versionNumber: 3,
    locale: 'en-GB',
    children: <Dossier />,
  },
  decorators: [
    (Story) => (
      <div style={{ height: 520, display: 'flex' }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof CanvasPane>

export default meta
type Story = StoryObj<typeof meta>

/** The wide layout: title, type and meta, version chip, reveal, export, close. */
export const Wide: Story = {
  args: {
    onToggleHistory: fn(),
    onExport: fn(),
    onRequestClose: fn(),
    onReveal: fn(),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('heading', { level: 2, name: 'Sister Ondrey Vashe' })).toBeInTheDocument()
    await expect(canvas.getByText('NPC Dossier')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'v3 — version history' })).toHaveAttribute('aria-expanded', 'false')
    await expect(canvas.getByRole('button', { name: 'Export' })).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Close canvas' })).toBeInTheDocument()
  },
}

// ── CANVAS-13 · the five save statuses ───────────────────────────────────────

export const StatusSaved: Story = {
  args: { saveStatus: 'saved' },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('status')).toHaveTextContent('Saved')
  },
}

export const StatusSaving: Story = {
  args: { saveStatus: 'saving' },
  play: async ({ canvasElement }) => {
    const live = within(canvasElement).getByRole('status')
    await expect(live).toHaveAttribute('aria-live', 'polite')
    await expect(live).toHaveTextContent('Saving…')
  },
}

export const StatusUnsaved: Story = {
  args: { saveStatus: 'unsaved' },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('status')).toHaveTextContent('Unsaved changes')
  },
}

/** STATE-2: the failure names its next action, and the action is really there. */
export const StatusFailed: Story = {
  args: { saveStatus: 'error', onRetrySave: fn() },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent("Couldn't save — Retry")
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await expect(args.onRetrySave).toHaveBeenCalledTimes(1)
  },
}

export const StatusConflict: Story = {
  args: { saveStatus: 'conflict' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('status')).toHaveTextContent('Conflict — review')
    // CANVAS-13 asks for exactly one aggregate status, not one per field.
    await expect(canvas.getAllByRole('status')).toHaveLength(1)
  },
}

// ── REVEAL-13 / REVEAL-14 · what the header says about exposure ──────────────

/** Nothing is live, so the indicator is absent (§12.2) and only Reveal shows. */
export const RevealHidden: Story = {
  args: { reveal: { state: 'hidden' }, onReveal: fn(), onStopReveal: fn() },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Reveal to party' })).toBeInTheDocument()
    await expect(canvas.queryByRole('button', { name: 'Stop showing' })).not.toBeInTheDocument()
  },
}

/** Two controls, never the handoff's one pill: a stray click cannot stop a reveal. */
export const RevealLive: Story = {
  args: {
    reveal: { state: 'revealed', summary: 'portrait, name & voice' },
    onReveal: fn(),
    onStopReveal: fn(),
  },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByText('Revealed · portrait, name & voice'))
    await expect(args.onStopReveal).not.toHaveBeenCalled()
    await userEvent.click(canvas.getByRole('button', { name: 'Change what the table sees' }))
    await expect(args.onReveal).toHaveBeenCalledTimes(1)
    await userEvent.click(canvas.getByRole('button', { name: 'Stop showing' }))
    await expect(args.onStopReveal).toHaveBeenCalledTimes(1)
  },
}

/** REVEAL-8 — the table is pinned to an earlier version whose text differs. */
export const RevealBehindLatest: Story = {
  args: {
    reveal: { state: 'revealed', summary: 'notes', behindLatest: true },
    onReveal: fn(),
    onStopReveal: fn(),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Table is seeing an earlier version')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Update…' })).toBeInTheDocument()
  },
}

/** REVEAL-13 — it never reads as "nothing revealed", and Stop stays reachable. */
export const RevealUnknown: Story = {
  args: { reveal: { state: 'unknown' }, onReveal: fn(), onStopReveal: fn() },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
    await expect(canvas.queryByRole('button', { name: 'Reveal to party' })).not.toBeInTheDocument()
  },
}

// ── CANVAS-27 · the history disclosure ───────────────────────────────────────

/** A disclosure, opened and closed from the version chip. */
export const HistoryDisclosure: Story = {
  render: function HistoryDisclosureStory(args) {
    const [open, setOpen] = React.useState(false)
    return <CanvasPane {...args} historyOpen={open} onToggleHistory={() => setOpen((was) => !was)} />
  },
  args: { history: { versions: VERSIONS, onRestore: fn() } },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const chip = canvas.getByRole('button', { name: 'v3 — version history' })
    await expect(chip).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(chip)
    await expect(chip).toHaveAttribute('aria-expanded', 'true')
    await expect(canvas.getByRole('heading', { level: 3, name: 'Version history' })).toBeInTheDocument()
    const rows = canvas.getAllByRole('listitem')
    await expect(rows[0]).toHaveAttribute('aria-current', 'true')
    await expect(within(rows[1]).getByText('You')).toBeInTheDocument()
    await userEvent.click(chip)
    await expect(canvas.queryByRole('listitem')).not.toBeInTheDocument()
  },
}

/** CANVAS-26 — restore is additive, so it runs with no further confirmation. */
export const HistoryRestore: Story = {
  args: { historyOpen: true, onToggleHistory: fn(), history: { versions: VERSIONS, onRestore: fn() } },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Restore v2' }))
    await expect(canvas.queryByRole('dialog')).not.toBeInTheDocument()
    await expect(args.history?.onRestore).toHaveBeenCalledWith(expect.objectContaining({ number: 2 }))
  },
}

export const HistoryEmpty: Story = {
  args: { historyOpen: true, onToggleHistory: fn(), history: { versions: [] } },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('Only one version so far')).toBeInTheDocument()
  },
}

export const HistoryLoading: Story = {
  args: { historyOpen: true, onToggleHistory: fn(), history: { status: 'loading' } },
  play: async ({ canvasElement }) => {
    const statuses = within(canvasElement).getAllByRole('status')
    await expect(statuses.some((node) => node.textContent?.includes('Loading version history…') === true)).toBe(true)
  },
}

export const HistoryFailed: Story = {
  args: { historyOpen: true, onToggleHistory: fn(), history: { status: 'error', onRetry: fn() } },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('alert')).toHaveTextContent("Couldn't load history")
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await expect(args.history?.onRetry).toHaveBeenCalled()
  },
}

export const HistoryPaged: Story = {
  args: {
    historyOpen: true,
    onToggleHistory: fn(),
    history: {
      versions: Array.from({ length: 20 }, (_, at) => version({ number: 100 - at })),
      currentVersionNumber: 100,
      hasMore: true,
      onLoadMore: fn(),
    },
  },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getAllByRole('listitem')).toHaveLength(20)
    const loadMore = canvas.getByRole('button', { name: 'Load more' })
    await userEvent.click(loadMore)
    await expect(args.history?.onLoadMore).toHaveBeenCalledTimes(1)
    await expect(loadMore).toHaveFocus()
  },
}

// ── CANVAS-29 · the gold wash ────────────────────────────────────────────────

export const ChangeArrived: Story = {
  args: { changeHighlight: true, onAcknowledgeChange: fn() },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Got it' }))
    await expect(args.onAcknowledgeChange).toHaveBeenCalledTimes(1)
  },
}

// ── CANVAS-32 · closing, and where focus lands ───────────────────────────────

/** Focus returns to the control that opened the canvas. */
export const CloseReturnsFocus: Story = {
  render: function CloseReturnsFocusStory(args) {
    const opener = React.useRef<HTMLButtonElement>(null)
    const composer = React.useRef<HTMLTextAreaElement>(null)
    const [open, setOpen] = React.useState(true)
    return (
      <div style={{ display: 'flex', gap: 16, flex: 1 }}>
        <div style={{ display: 'grid', gap: 8, alignContent: 'start', padding: 16, width: 220 }}>
          <button type="button" ref={opener} onClick={() => setOpen(true)}>
            Open in canvas
          </button>
          <textarea aria-label="Message the assistant" ref={composer} rows={3} />
        </div>
        {open && (
          <CanvasPane
            {...args}
            openerRef={opener}
            composerRef={composer}
            onRequestClose={() => {
              setOpen(false)
            }}
          />
        )}
      </div>
    )
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'Close canvas' }))
    await waitFor(async () => {
      await expect(canvas.getByRole('button', { name: 'Open in canvas' })).toHaveFocus()
    })
  },
}

/** CANVAS-16: the owner may refuse the close, and then nothing moves. */
export const CloseRefusedByTheGuard: Story = {
  render: function CloseRefusedStory(args) {
    const opener = React.useRef<HTMLButtonElement>(null)
    return (
      <div style={{ display: 'flex', gap: 16, flex: 1 }}>
        <div style={{ padding: 16, width: 220 }}>
          <button type="button" ref={opener}>
            Open in canvas
          </button>
        </div>
        <CanvasPane {...args} openerRef={opener} onRequestClose={() => false} />
      </div>
    )
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const close = canvas.getByRole('button', { name: 'Close canvas' })
    await userEvent.click(close)
    await expect(close).toHaveFocus()
    await expect(canvas.getByRole('button', { name: 'Open in canvas' })).not.toHaveFocus()
  },
}

// ── §10.2 · the other two canvas-header presentations ────────────────────────

/** Below 560 px of canvas width: the meta line hides, Reveal becomes an icon. */
export const Compact: Story = {
  args: {
    layout: 'compact',
    onReveal: fn(),
    onStopReveal: fn(),
    onExport: fn(),
    onRequestClose: fn(),
    onToggleHistory: fn(),
    reveal: { state: 'revealed', summary: 'name' },
  },
  decorators: [
    (Story) => (
      <div style={{ height: 520, width: 520, display: 'flex' }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvasElement.querySelector('.gm-canvas__meta--offscreen')).not.toBeNull()
    // Stop showing keeps its text on every layout.
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toHaveClass('aether-btn')
    await expect(canvas.getByRole('button', { name: 'Change what the table sees' })).toHaveClass('aether-icon-btn')
  },
}

/** The narrow layout: a back control, and export and history in an overflow menu. */
export const Narrow: Story = {
  args: {
    layout: 'fullScreen',
    onExport: fn(),
    onToggleHistory: fn(),
    onRequestClose: fn(),
    onReveal: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ height: 620, width: 375, display: 'flex' }}>
        <Story />
      </div>
    ),
  ],
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'Close canvas' })).not.toBeInTheDocument()
    await expect(canvas.getByRole('button', { name: 'Back' })).toBeInTheDocument()

    const trigger = canvas.getByRole('button', { name: 'More canvas actions' })
    await userEvent.click(trigger)
    const menu = canvas.getByRole('menu', { name: 'Canvas actions' })
    await waitFor(async () => {
      await expect(within(menu).getByRole('menuitem', { name: 'Version history' })).toHaveFocus()
    })
    await userEvent.keyboard('{ArrowDown}')
    await expect(within(menu).getByRole('menuitem', { name: 'Export' })).toHaveFocus()
    await userEvent.keyboard('{Escape}')
    await expect(canvas.queryByRole('menu')).not.toBeInTheDocument()
    await expect(trigger).toHaveFocus()

    await userEvent.click(trigger)
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Export' }))
    await expect(args.onExport).toHaveBeenCalledTimes(1)
  },
}

// ── Both themes, from tokens only ────────────────────────────────────────────

/** The relative luminance of a `rgb(r, g, b)` string, for the theme checks. */
function luminance(colour: string): number {
  const [r, g, b] = (colour.match(/\d+(\.\d+)?/g) ?? ['0', '0', '0']).slice(0, 3).map(Number)
  const channel = (value: number): number => {
    const c = value / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** Light Parchment — the `:root` default. */
export const ThemeLight: Story = {
  globals: { theme: 'light' },
  args: { onReveal: fn(), onExport: fn(), onRequestClose: fn(), onToggleHistory: fn() },
  play: async ({ canvasElement }) => {
    const pane = canvasElement.querySelector('.gm-canvas')
    await expect(pane).not.toBeNull()
    await waitFor(() => {
      expect(luminance(getComputedStyle(pane as Element).backgroundColor)).toBeGreaterThan(0.5)
    })
  },
}

/** Dark Tavern — the same markup, the same tokens, no per-theme CSS of its own. */
export const ThemeDark: Story = {
  globals: { theme: 'dark' },
  args: {
    onReveal: fn(),
    onExport: fn(),
    onRequestClose: fn(),
    onToggleHistory: fn(),
    historyOpen: true,
    history: { versions: VERSIONS, onRestore: fn() },
    reveal: { state: 'revealed', summary: 'portrait, name & voice' },
    onStopReveal: fn(),
  },
  play: async ({ canvasElement }) => {
    const pane = canvasElement.querySelector('.gm-canvas')
    await expect(pane).not.toBeNull()
    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
      expect(luminance(getComputedStyle(pane as Element).backgroundColor)).toBeLessThan(0.5)
    })
    // The controls are still there, and still named, in the other theme.
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Stop showing' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'Restore v2' })).toBeVisible()
  },
}

/** X-8 — a document type this bundle does not know is neutral, never an NPC. */
export const UnknownDocumentType: Story = {
  args: { documentType: 'grimoire', title: 'Something from a newer Aetheril' },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByText('Document')).toBeInTheDocument()
    await expect(canvas.queryByText('NPC Dossier')).not.toBeInTheDocument()
  },
}
