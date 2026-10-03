/**
 * CanvasHostLifecycle.test.tsx -- the canvas column's lifecycle states (agent-forge-harness-1kg.6.4,
 * PR-2; LIB-16, section 12.2). The `Archived` banner with Restore, and Back to library on the
 * unavailable and failed states. Real providers, a recording server.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LibraryPanelProvider, useLibraryPanel } from '../shell/libraryPanel'
import {
  defaultWorkbenchRoute, documentBody, live, mountSelected, run, unarchiveRoute, type Route,
} from '../testing/workbenchHarness'
import { CanvasHost } from './CanvasHost'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

let panelApi: ReturnType<typeof useLibraryPanel>

function Probe(): null {
  const panel = useLibraryPanel()
  React.useLayoutEffect(() => {
    panelApi = panel
  })
  return null
}

const host = (server: { fetchImpl: typeof fetch }): React.ReactElement => (
  <LibraryPanelProvider>
    <Probe />
    <CanvasHost fetchImpl={server.fetchImpl} />
  </LibraryPanelProvider>
)

const open = (documentId: string, title: string | null = null) =>
  live.actions.openDocument({ documentId, title }, { gesture: true })

/** `doc_a` is archived until an unarchive call lands; `restoreAnswer` replaces that call's reply. */
function archivedRoute(options: { restoreAnswer?: () => { status: number; body?: unknown } | 'defer' | undefined } = {}): Route {
  let archived = true
  return unarchiveRoute({
    answer: () => {
      const override = options.restoreAnswer?.()
      if (override !== undefined) return override
      archived = false
      return undefined
    },
    fallback: (call) => {
      if (call.method === 'GET' && /\/documents\/doc_a$/.test(call.url)) {
        return { status: 200, body: documentBody('cmp_A', 'doc_a', { archived }) }
      }
      return defaultWorkbenchRoute(call)
    },
  })
}

describe('the Archived banner (LIB-16)', () => {
  it('an open archived document says so under a labelled group, with Restore, and the document is still shown', async () => {
    await mountSelected(host, { route: archivedRoute() })
    await run(() => open('doc_a'))
    const banner = await screen.findByRole('group', { name: 'Archived' })
    expect(banner).toHaveTextContent('This document is archived. It is hidden from the library lists.')
    expect(within(banner).getByRole('button', { name: 'Restore' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Ondrey' })).toBeInTheDocument()
  })

  it('a document that is not archived has no banner', async () => {
    await mountSelected(host)
    await run(() => open('doc_a'))
    await screen.findByRole('heading', { name: 'Ondrey' })
    expect(screen.queryByRole('group', { name: 'Archived' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull()
  })

  it('Restore unarchives, reads the document again, removes the banner, announces it and lands on the title', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(host, { route: archivedRoute() })
    await run(() => open('doc_a'))
    await user.click(await screen.findByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Archived' })).toBeNull())
    expect(server.unarchiveCalls()).toHaveLength(1)
    expect(server.unarchiveCalls()[0]).toMatchObject({ url: '/campaigns/cmp_A/documents/doc_a/unarchive', method: 'POST', body: null })
    expect(server.docCalls()).toHaveLength(2)
    expect(live.state.doc.kind === 'open' && live.state.doc.document.archived).toBe(false)
    expect(live.state.announcement).toBe('Restored Ondrey')
    expect(screen.getByRole('heading', { name: 'Ondrey' })).toHaveFocus()
  })

  it('Restore bumps the documents version, so every library list asks again', async () => {
    const user = userEvent.setup()
    await mountSelected(host, { route: archivedRoute() })
    await run(() => open('doc_a'))
    const before = live.state.documentsVersion
    await user.click(await screen.findByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(live.state.documentsVersion).toBe(before + 1))
  })

  it('while Restore is in flight the button is aria-disabled and a second press sends nothing', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(host, { route: archivedRoute({ restoreAnswer: () => 'defer' }) })
    await run(() => open('doc_a'))
    await user.click(await screen.findByRole('button', { name: 'Restore' }))
    const busy = await screen.findByRole('button', { name: 'Restoring…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    await user.click(busy)
    expect(server.unarchiveCalls()).toHaveLength(1)
  })

  it.each([
    ['a 429', { status: 429, body: {} }, 'Too many changes at once. Wait a moment and try again.'],
    ['a 503', { status: 503, body: {} }, "Couldn't restore Ondrey. Nothing changed."],
  ])('%s says why inside the banner, keeps it, and Restore can be pressed again', async (_label, reply, text) => {
    const user = userEvent.setup()
    let first = true
    const { server } = await mountSelected(host, {
      route: archivedRoute({
        restoreAnswer: () => {
          if (!first) return undefined
          first = false
          return reply
        },
      }),
    })
    await run(() => open('doc_a'))
    await user.click(await screen.findByRole('button', { name: 'Restore' }))
    expect(await within(screen.getByRole('group', { name: 'Archived' })).findByText(text)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Archived' })).toBeNull())
    expect(server.unarchiveCalls()).toHaveLength(2)
  })

  it('a document the server no longer has: Restore says it is not available and the canvas shows the unavailable state', async () => {
    const user = userEvent.setup()
    let gone = false
    const route: Route = (call) => {
      if (/\/unarchive$/.test(call.url)) {
        gone = true
        return { status: 404, body: {} }
      }
      if (gone && call.method === 'GET' && /\/documents\/doc_a$/.test(call.url)) return { status: 404, body: {} }
      return archivedRoute()(call)
    }
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await user.click(await screen.findByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('unavailable'))
  })
})

describe('refreshDocument', () => {
  it('replaces the open document in place with no loading state, and only for the open document', async () => {
    let archived = false
    const route: Route = (call) =>
      call.method === 'GET' && /\/documents\/doc_a$/.test(call.url)
        ? { status: 200, body: documentBody('cmp_A', 'doc_a', { archived }) }
        : defaultWorkbenchRoute(call)
    const { server } = await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await screen.findByRole('heading', { name: 'Ondrey' })
    archived = true
    const kinds: string[] = []
    const stop = setInterval(() => kinds.push(live.state.doc.kind), 0)
    await run(() => live.actions.refreshDocument('doc_a'))
    clearInterval(stop)
    expect(kinds.every((kind) => kind === 'open')).toBe(true)
    expect(live.state.doc.kind === 'open' && live.state.doc.document.archived).toBe(true)
    const calls = server.docCalls().length
    await run(() => live.actions.refreshDocument('doc_b'))
    expect(server.docCalls()).toHaveLength(calls)
  })
})

describe('Back to library (section 12.2)', () => {
  it('the unavailable state offers it beside Close, and it opens the library on the tab last used', async () => {
    const user = userEvent.setup()
    await mountSelected(host)
    act(() => panelApi.setCategory('session-log'))
    await run(() => open('doc_missing'))
    const panel = await screen.findByRole('heading', { name: "This document isn't available" })
    expect(panel).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
    expect(panelApi.open).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Back to library' }))
    expect(panelApi.open).toBe(true)
    expect(panelApi.category).toBe('session-log')
  })

  it('the failed state offers it beside Retry and Close', async () => {
    const user = userEvent.setup()
    const route: Route = (call) =>
      call.method === 'GET' && /\/documents\/doc_a$/.test(call.url) ? { status: 503, body: {} } : defaultWorkbenchRoute(call)
    await mountSelected(host, { route })
    await run(() => open('doc_a', 'Ondrey'))
    await screen.findByRole('heading', { name: "Couldn't open Ondrey" })
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Back to library' }))
    expect(panelApi.open).toBe(true)
  })

  it('the unsupported state does not offer it, and an open document does not either', async () => {
    await mountSelected(host)
    await run(() => open('doc_newer'))
    await screen.findByRole('heading', { name: 'This document was made by a newer version of Aetheril.' })
    expect(screen.queryByRole('button', { name: 'Back to library' })).toBeNull()
    await run(() => open('doc_a'))
    await screen.findByRole('heading', { name: 'Ondrey' })
    expect(screen.queryByRole('button', { name: 'Back to library' })).toBeNull()
  })
})
