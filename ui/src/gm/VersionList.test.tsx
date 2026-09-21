/**
 * VersionList — CANVAS-27's history list and CANVAS-26's additive restore.
 *
 * Covers: newest-first order, the marked current entry, author labels, the
 * derived label, the client-formatted time with its machine-readable
 * `<time dateTime>`, changed fields, 20-per-page paging with sensible focus,
 * the empty / loading / error rows of §12.2, and the restore affordance.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

const THREE: DocumentVersion[] = [
  version({ number: 3, author: 'assistant', summary: 'Wants the signet', changed_fields: ['wants'] }),
  version({ number: 2, author: 'gm', summary: 'Voice, by hand', created_at: '2026-09-16T19:34:00Z' }),
  version({ number: 1, author: 'assistant', summary: 'First draft', created_at: '2026-09-16T19:31:00Z' }),
]

function page(size: number, from = 100): DocumentVersion[] {
  return Array.from({ length: size }, (_, at) => version({ number: from - at }))
}

describe('CANVAS-27 — what a history row shows', () => {
  it('renders the versions in the order given, newest first', () => {
    render(<VersionList versions={THREE} currentVersionNumber={3} />)
    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveTextContent('v3')
    expect(rows[1]).toHaveTextContent('v2')
    expect(rows[2]).toHaveTextContent('v1')
  })

  it('shows the derived label, the summary and the author as You or Assistant', () => {
    render(<VersionList versions={THREE} currentVersionNumber={3} />)
    const gmRow = screen.getAllByRole('listitem')[1]
    expect(gmRow).toHaveTextContent('v2')
    expect(gmRow).toHaveTextContent('Voice, by hand')
    expect(within(gmRow).getByText('You')).toBeInTheDocument()
    expect(within(screen.getAllByRole('listitem')[0]).getByText('Assistant')).toBeInTheDocument()
  })

  it('formats the time on the client and keeps the ISO value machine-readable', () => {
    const { container } = render(<VersionList versions={THREE} currentVersionNumber={3} locale="en-GB" />)
    const times = container.querySelectorAll('time')
    expect(times).toHaveLength(3)
    expect(times[0]).toHaveAttribute('datetime', '2026-09-16T19:36:00Z')
    // The handoff's pre-formatted "7:36 PM" is rejected: a real format shows a date.
    expect(times[0].textContent).toContain('2026')
    expect(times[0].textContent).not.toBe('2026-09-16T19:36:00Z')
  })

  it('shows a timestamp it cannot format as plain text, with no <time> element', () => {
    const { container } = render(
      <VersionList versions={[version({ number: 1, created_at: '7:36 PM' })]} currentVersionNumber={1} />,
    )
    expect(container.querySelector('time')).toBeNull()
    expect(screen.getByText('7:36 PM')).toBeInTheDocument()
  })

  it('names the changed fields with the registry label for the document type', () => {
    render(
      <VersionList
        versions={[version({ number: 4, changed_fields: ['if_attacked', 'name'] })]}
        currentVersionNumber={4}
        documentType="npc"
      />,
    )
    expect(screen.getByText(/If the party attacks, Name/)).toBeInTheDocument()
  })

  it('marks the current entry with aria-current and a visible word', () => {
    render(<VersionList versions={THREE} currentVersionNumber={2} />)
    const rows = screen.getAllByRole('listitem')
    expect(rows[1]).toHaveAttribute('aria-current', 'true')
    expect(rows[0]).not.toHaveAttribute('aria-current')
    expect(within(rows[1]).getByText('Current')).toBeInTheDocument()
  })

  it('names the version a restore came from', () => {
    render(
      <VersionList versions={[version({ number: 4, restored_from: 1 })]} currentVersionNumber={4} />,
    )
    expect(screen.getByText('Restored from v1')).toBeInTheDocument()
  })

  it('is named by the element the caller points at', () => {
    render(
      <>
        <h3 id="history-heading">Version history</h3>
        <VersionList versions={THREE} currentVersionNumber={3} labelledBy="history-heading" />
      </>,
    )
    expect(screen.getByRole('list', { name: 'Version history' })).toBeInTheDocument()
  })
})

describe('CANVAS-27 — paging, 20 at a time', () => {
  it('offers Load more only while the server says there is another page', () => {
    const { rerender } = render(<VersionList versions={page(HISTORY_PAGE_SIZE)} currentVersionNumber={100} />)
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
    rerender(<VersionList versions={page(HISTORY_PAGE_SIZE)} currentVersionNumber={100} hasMore />)
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
  })

  it('asks for the next page and appends it without reordering', async () => {
    const onLoadMore = vi.fn()
    const first = page(HISTORY_PAGE_SIZE)
    const { rerender } = render(
      <VersionList versions={first} currentVersionNumber={100} hasMore onLoadMore={onLoadMore} />,
    )
    expect(screen.getAllByRole('listitem')).toHaveLength(HISTORY_PAGE_SIZE)
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    expect(onLoadMore).toHaveBeenCalledTimes(1)

    const both = [...first, ...page(5, 80)]
    rerender(<VersionList versions={both} currentVersionNumber={100} hasMore onLoadMore={onLoadMore} />)
    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(HISTORY_PAGE_SIZE + 5)
    expect(rows[0]).toHaveTextContent('v100')
    expect(rows[HISTORY_PAGE_SIZE]).toHaveTextContent('v80')
  })

  it('keeps focus on Load more while the button survives the next page', async () => {
    const first = page(HISTORY_PAGE_SIZE)
    const { rerender } = render(
      <VersionList versions={first} currentVersionNumber={100} hasMore onLoadMore={vi.fn()} />,
    )
    const button = screen.getByRole('button', { name: 'Load more' })
    await userEvent.click(button)
    rerender(
      <VersionList versions={[...first, ...page(5, 80)]} currentVersionNumber={100} hasMore onLoadMore={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: 'Load more' })).toHaveFocus()
  })

  it('moves focus to the first row of the last page once Load more is gone', async () => {
    const first = page(HISTORY_PAGE_SIZE)
    const { rerender } = render(
      <VersionList versions={first} currentVersionNumber={100} hasMore onLoadMore={vi.fn()} />,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    rerender(<VersionList versions={[...first, ...page(3, 80)]} currentVersionNumber={100} />)
    const rows = screen.getAllByRole('listitem')
    expect(rows[HISTORY_PAGE_SIZE]).toHaveTextContent('v80')
    expect(rows[HISTORY_PAGE_SIZE]).toHaveFocus()
  })

  it('shows inline progress while the next page is in flight and blocks a second press', async () => {
    const onLoadMore = vi.fn()
    render(<VersionList versions={page(HISTORY_PAGE_SIZE)} currentVersionNumber={100} hasMore loadingMore onLoadMore={onLoadMore} />)
    const button = screen.getByRole('button', { name: 'Loading…' })
    expect(button).toBeDisabled()
    await userEvent.click(button)
    expect(onLoadMore).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('Loading older versions…')
  })

  it('leaves focus alone when a page arrives that nobody asked for', () => {
    const first = page(HISTORY_PAGE_SIZE)
    const { rerender } = render(<VersionList versions={first} currentVersionNumber={100} />)
    rerender(<VersionList versions={[...first, ...page(3, 80)]} currentVersionNumber={100} />)
    expect(document.body).toHaveFocus()
  })
})

describe('§12.2 — the history list’s empty, loading and error rows', () => {
  it('says Only one version so far when there is nothing to page through', () => {
    render(<VersionList versions={[]} currentVersionNumber={1} />)
    expect(screen.getByText('Only one version so far')).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('shows a skeleton plus a polite message while loading (STATE-4)', () => {
    const { container } = render(<VersionList status="loading" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading version history…')
    expect(container.querySelectorAll('.gm-versions__skeleton-row').length).toBeGreaterThan(0)
  })

  it("says Couldn't load history and offers Retry (STATE-2)", async () => {
    const onRetry = vi.fn()
    render(<VersionList status="error" onRetry={onRetry} />)
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load history")
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('does not name a Retry that is not wired', () => {
    render(<VersionList status="error" />)
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })
})

describe('CANVAS-26 — restore is additive', () => {
  it('offers a restore on every older version and never on the current one', () => {
    render(<VersionList versions={THREE} currentVersionNumber={3} onRestore={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Restore v2' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Restore v1' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restore v3' })).not.toBeInTheDocument()
  })

  it('restores straight away — nothing is lost, so the record asks for no dialog', async () => {
    const onRestore = vi.fn()
    render(<VersionList versions={THREE} currentVersionNumber={3} onRestore={onRestore} />)
    await userEvent.click(screen.getByRole('button', { name: 'Restore v1' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onRestore).toHaveBeenCalledTimes(1)
    expect(onRestore.mock.calls[0][0]).toMatchObject({ number: 1, summary: 'First draft' })
  })

  it('renders no restore affordance when the caller wires none', () => {
    render(<VersionList versions={THREE} currentVersionNumber={3} />)
    expect(screen.queryByRole('button', { name: /^Restore/ })).not.toBeInTheDocument()
  })

  it('announces the restore in flight, keeps its button focusable and stops the others', async () => {
    const onRestore = vi.fn()
    render(
      <VersionList versions={THREE} currentVersionNumber={3} onRestore={onRestore} restoringVersionNumber={1} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Restoring v1…')
    const busy = screen.getByRole('button', { name: 'Restoring v1…' })
    expect(busy).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Restore v2' })).toBeDisabled()
    await userEvent.click(busy)
    expect(onRestore).not.toHaveBeenCalled()
  })
})
