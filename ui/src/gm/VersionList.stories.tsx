import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fn, userEvent, within } from 'storybook/test'

import { VersionList } from './VersionList'
import { HISTORY_PAGE_SIZE } from './canvasStatus'
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

const ONDREY: DocumentVersion[] = [
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

const meta = {
  title: 'GM Workbench/VersionList',
  component: VersionList,
  parameters: { layout: 'padded' },
  args: {
    versions: ONDREY,
    currentVersionNumber: 3,
    documentType: 'npc',
    locale: 'en-GB',
  },
  decorators: [
    (Story) => (
      <div style={{ maxWidth: 420 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof VersionList>

export default meta
type Story = StoryObj<typeof meta>

/** CANVAS-27: label, summary, author, a client-formatted time and changed fields. */
export const Playground: Story = {
  args: { onRestore: fn() },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const rows = canvas.getAllByRole('listitem')
    await expect(rows).toHaveLength(3)
    await expect(rows[0]).toHaveAttribute('aria-current', 'true')
    await expect(within(rows[1]).getByText('You')).toBeInTheDocument()
    await expect(within(rows[0]).getByText('Assistant')).toBeInTheDocument()
    const time = rows[0].querySelector('time')
    await expect(time).toHaveAttribute('datetime', '2026-09-16T19:36:00Z')
    await expect(time?.textContent).toContain('2026')
  },
}

/** CANVAS-26: restore is additive, so it runs straight away — no dialog. */
export const Restore: Story = {
  args: { onRestore: fn() },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.queryByRole('button', { name: 'Restore v3' })).not.toBeInTheDocument()
    await userEvent.click(canvas.getByRole('button', { name: 'Restore v1' }))
    await expect(canvas.queryByRole('dialog')).not.toBeInTheDocument()
    await expect(args.onRestore).toHaveBeenCalledWith(expect.objectContaining({ number: 1 }))
  },
}

/** One restore at a time; the busy row keeps its focus and says what it is doing. */
export const Restoring: Story = {
  args: { onRestore: fn(), restoringVersionNumber: 1 },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Restoring v1…' })).toBeEnabled()
    await expect(canvas.getByRole('button', { name: 'Restore v2' })).toBeDisabled()
  },
}

/** §12.2 — nothing to page through yet. */
export const Empty: Story = {
  args: { versions: [] },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByText('Only one version so far')).toBeInTheDocument()
  },
}

/** STATE-4 — a skeleton plus a polite message, never a spinner alone. */
export const Loading: Story = {
  args: { status: 'loading', versions: [] },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).getByRole('status')).toHaveTextContent('Loading version history…')
  },
}

/** §12.2 / STATE-2 — the failure names its next action. */
export const Failed: Story = {
  args: { status: 'error', versions: [], onRetry: fn() },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('alert')).toHaveTextContent("Couldn't load history")
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await expect(args.onRetry).toHaveBeenCalled()
  },
}

/** CANVAS-27 — 20 to a page, behind Load more rather than infinite scroll. */
export const Paged: Story = {
  args: {
    versions: Array.from({ length: HISTORY_PAGE_SIZE }, (_, at) => version({ number: 100 - at })),
    currentVersionNumber: 100,
    hasMore: true,
    onLoadMore: fn(),
  },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getAllByRole('listitem')).toHaveLength(HISTORY_PAGE_SIZE)
    const loadMore = canvas.getByRole('button', { name: 'Load more' })
    await userEvent.click(loadMore)
    await expect(args.onLoadMore).toHaveBeenCalledTimes(1)
    await expect(loadMore).toHaveFocus()
  },
}

/** The next page is in flight: inline progress, and a second press cannot slip through. */
export const PagingInFlight: Story = {
  args: {
    versions: Array.from({ length: HISTORY_PAGE_SIZE }, (_, at) => version({ number: 100 - at })),
    currentVersionNumber: 100,
    hasMore: true,
    loadingMore: true,
    onLoadMore: fn(),
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole('button', { name: 'Loading…' })).toBeDisabled()
    await expect(canvas.getByRole('status')).toHaveTextContent('Loading older versions…')
  },
}
