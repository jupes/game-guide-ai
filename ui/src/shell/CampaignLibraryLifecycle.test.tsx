/**
 * CampaignLibraryLifecycle.test.tsx -- the Campaign Library's lifecycle (agent-forge-harness-1kg.6.4,
 * PR-2; LIB-12, LIB-16, LIB-17, LIB-18, LIB-24, SEC-40, STATE-3).
 *
 * The REAL providers and a recording server, as `CampaignLibraryPanel.test.tsx`. jsdom owns
 * semantics: roles, names, focus, which calls are made and what each carries. Boxes and contrast
 * are Chromium's, in `CampaignLibraryPanel.stories.tsx`.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { liveFixture } from '../gm/revealFixtures'
import {
  archiveRoute, createRoute, DELETE_PASSWORD, defaultWorkbenchRoute, deleteRoute, documentBody, flush, libraryRoute, live,
  mountSelected, revealPicture, unarchiveRoute, type Call, type LibraryRow, type Reply, type Route,
} from '../testing/workbenchHarness'
import { CampaignLibraryPanel, UNDO_MS } from './CampaignLibraryPanel'
import { LibraryPanelProvider, useLibraryPanel, type LibraryCategoryId } from './libraryPanel'
import { CanvasHost } from '../gm/CanvasHost'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

let panelApi: ReturnType<typeof useLibraryPanel>

function Host({ fetchImpl }: { fetchImpl: typeof fetch }): React.JSX.Element | null {
  const panel = useLibraryPanel()
  React.useLayoutEffect(() => {
    panelApi = panel
  })
  return panel.open ? <CampaignLibraryPanel layout="wide" fetchImpl={fetchImpl} /> : null
}

async function mountPanel(route: Route, category: LibraryCategoryId = 'npcs', withCanvas = false) {
  const mounted = await mountSelected(
    (server) => (
      <LibraryPanelProvider>
        <Host fetchImpl={server.fetchImpl} />
        {withCanvas && <CanvasHost fetchImpl={server.fetchImpl} />}
      </LibraryPanelProvider>
    ),
    { route },
  )
  act(() => panelApi.openLibrary(category, null))
  await screen.findByRole('region', { name: 'Campaign Library' })
  return mounted
}

const ROWS: Readonly<Record<string, readonly LibraryRow[]>> = {
  npcs: [
    { id: 'doc_a', type: 'npc', title: 'Ondrey', qualifier: 'Ferryman' },
    { id: 'doc_b', type: 'npc', title: 'Brannoch' },
    { id: 'doc_v', type: 'npc', title: 'Velka', archived: true },
    { id: 'doc_w', type: 'npc', title: 'Wren', archived: true },
  ],
  bestiary: [{ id: 'doc_t', type: 'statblock', title: 'Tidewarden' }],
}

type Answer = (documentId: string, call: Call) => Reply | 'defer' | undefined

/** Every lifecycle route, over a library of `ROWS`. Each takes its own `answer` to fail a call. */
function lifecycle(options: {
  archive?: Answer
  unarchive?: Answer
  remove?: Answer
  fallback?: Route
} = {}): Route {
  return archiveRoute({
    answer: options.archive,
    fallback: unarchiveRoute({
      answer: options.unarchive,
      fallback: deleteRoute({
        answer: options.remove,
        fallback: createRoute({ fallback: libraryRoute(ROWS, { fallback: options.fallback ?? defaultWorkbenchRoute }) }),
      }),
    }),
  })
}

const panel = (): HTMLElement => screen.getByRole('region', { name: 'Campaign Library' })
/** The tab panel only: the status node, which repeats announced text, is outside it. */
const body = (): ReturnType<typeof within> => within(screen.getByRole('tabpanel'))
const status = (): HTMLElement => within(panel()).getByRole('status')
const tab = (name: string): HTMLElement => screen.getByRole('tab', { name })
const rowNames = (): string[] =>
  Array.from(panel().querySelectorAll('.library-row .library-row__title'), (title) => title.textContent ?? '')
const more = (title: string): HTMLElement => screen.getByRole('button', { name: `More actions for ${title}` })
const deleteBodies = (server: { deleteCalls: () => Array<{ body: string | null }> }): unknown[] =>
  server.deleteCalls().map((call) => JSON.parse(call.body ?? 'null'))

// ── Archive (LIB-16) ─────────────────────────────────────────────────────────

describe('the row overflow (LIB-16)', () => {
  it('every row has one, collapsed, naming its title; Active offers Archive and Archived offers Delete, never both', async () => {
    const user = userEvent.setup()
    await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const trigger = more('Ondrey')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('button', { name: 'Archive Ondrey' })).toBeNull()
    await user.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Archive Ondrey' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Delete/ })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(more('Velka'))
    expect(screen.getByRole('button', { name: 'Delete Velka' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Archive /})).toBeNull()
  })

  it('Escape closes it and returns focus to its trigger, and the panel stays open', async () => {
    const user = userEvent.setup()
    await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    act(() => screen.getByRole('button', { name: 'Archive Ondrey' }).focus())
    await user.keyboard('{Escape}')
    expect(more('Ondrey')).toHaveAttribute('aria-expanded', 'false')
    expect(more('Ondrey')).toHaveFocus()
    expect(panelApi.open).toBe(true)
  })

  it('moving focus out of the row closes it', async () => {
    const user = userEvent.setup()
    await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('textbox', { name: 'Search NPCs' }))
    expect(more('Ondrey')).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('Archive and its Undo (LIB-16)', () => {
  it('archives at once with no body, takes the row out, announces it and offers Undo; focus lands on the next row', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    await waitFor(() => expect(rowNames()).toEqual(['Brannoch']))
    expect(server.archiveCalls()).toHaveLength(1)
    expect(server.archiveCalls()[0]).toMatchObject({ url: '/campaigns/cmp_A/documents/doc_a/archive', method: 'POST', body: null })
    expect(screen.getByRole('button', { name: /^Brannoch/ })).toHaveFocus()
    expect(status()).toHaveTextContent('Archived Ondrey')
    const toast = screen.getByRole('group', { name: 'Undo archive' })
    expect(toast).toHaveTextContent('Archived Ondrey')
    expect(within(toast).getByRole('button', { name: 'Undo' })).toBeInTheDocument()
    // The toast is not a second live region: the one status node says it.
    expect(panel().querySelectorAll('[role="status"], [role="alert"], [aria-live]')).toHaveLength(1)
  })

  it('Undo unarchives the same document, brings the row back, says so and takes the toast away', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    await user.click(await screen.findByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(server.unarchiveCalls()).toHaveLength(1))
    expect(server.unarchiveCalls()[0].url).toBe('/campaigns/cmp_A/documents/doc_a/unarchive')
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Undo archive' })).toBeNull())
    expect(status()).toHaveTextContent('Restored Ondrey')
    // The archive took the row out in place; Undo bumps the documents version, which asks for the list again.
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(2))
    await waitFor(() => expect(rowNames()).toContain('Ondrey'))
    expect(within(panel()).getByRole('heading', { name: 'Campaign Library' })).toHaveFocus()
  })

  it('the toast goes after 8 seconds, waits while it is hovered, and starts again when the pointer leaves', async () => {
    const user = userEvent.setup()
    const timers = vi.spyOn(window, 'setTimeout')
    await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    const toast = await screen.findByRole('group', { name: 'Undo archive' })
    const undoTimers = (): Array<() => void> =>
      timers.mock.calls.filter(([, delay]) => delay === UNDO_MS).map(([callback]) => callback as () => void)
    expect(undoTimers()).toHaveLength(1)
    await user.hover(toast)
    await user.unhover(toast)
    expect(undoTimers()).toHaveLength(2)
    act(() => undoTimers()[1]())
    expect(screen.queryByRole('group', { name: 'Undo archive' })).toBeNull()
  })

  it('a throttled archive keeps the row, says why, and a failed one offers Retry that archives again', async () => {
    const user = userEvent.setup()
    const answers: Array<{ status: number; body: unknown } | undefined> = [{ status: 429, body: {} }, { status: 503, body: {} }, undefined]
    const { server } = await mountPanel(lifecycle({ archive: () => answers.shift() }))
    await screen.findByText('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    expect(await body().findByText('Too many changes at once. Wait a moment and try again.')).toBeInTheDocument()
    expect(rowNames()).toContain('Ondrey')
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    expect(await body().findByText("Couldn't archive Ondrey. Nothing changed.")).toBeInTheDocument()
    expect(status()).toHaveTextContent("Couldn't archive Ondrey. Nothing changed.")
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(rowNames()).toEqual(['Brannoch']))
    expect(server.archiveCalls()).toHaveLength(3)
  })

  it('archiving the document that is open in the canvas reads it again, so the canvas can say it is archived', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle(), 'npcs', true)
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: /^Ondrey/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(server.docCalls()).toHaveLength(1)
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    await waitFor(() => expect(server.docCalls()).toHaveLength(2))
  })
})

describe('Archive while the table is seeing the document (LIB-17)', () => {
  const liveRoute = (): Route => (call) =>
    call.method === 'GET' && /\/reveals$/.test(call.url)
      ? { status: 200, body: revealPicture({ table: liveFixture('doc_a', ['name']) }) }
      : lifecycle()(call)

  it('asks first, names the document and the consequence, and focuses Cancel; nothing is sent until Archive', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(liveRoute())
    await screen.findByText('Ondrey')
    await waitFor(() => expect(live.reveals.state).not.toBeNull())
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    const dialog = screen.getByRole('dialog', { name: 'Archive Ondrey?' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAccessibleDescription('The table is seeing this. Archiving stops showing it.')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    expect(server.archiveCalls()).toHaveLength(0)

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(more('Ondrey')).toHaveFocus()
    expect(server.archiveCalls()).toHaveLength(0)
  })

  it('Archive then archives, offers Undo, and reads the reveal picture again', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(liveRoute())
    await screen.findByText('Ondrey')
    await waitFor(() => expect(live.reveals.state).not.toBeNull())
    const revealReads = (): number => server.calls.filter((call) => /\/reveals$/.test(call.url)).length
    const before = revealReads()
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(server.archiveCalls()).toHaveLength(1))
    expect(await screen.findByRole('group', { name: 'Undo archive' })).toBeInTheDocument()
    await waitFor(() => expect(revealReads()).toBeGreaterThan(before))
  })

  it('a document that is not live archives with no dialog at all', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(liveRoute())
    await screen.findByText('Brannoch')
    await waitFor(() => expect(live.reveals.state).not.toBeNull())
    await user.click(more('Brannoch'))
    await user.click(screen.getByRole('button', { name: 'Archive Brannoch' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(server.archiveCalls()).toHaveLength(1))
  })

  it('Escape in the dialog cancels it and does not close the panel', async () => {
    const user = userEvent.setup()
    await mountPanel(liveRoute())
    await screen.findByText('Ondrey')
    await waitFor(() => expect(live.reveals.state).not.toBeNull())
    await user.click(more('Ondrey'))
    await user.click(screen.getByRole('button', { name: 'Archive Ondrey' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(panelApi.open).toBe(true)
  })
})

// ── Delete (LIB-18, SEC-40) ──────────────────────────────────────────────────

async function openDelete(user: ReturnType<typeof userEvent.setup>, title = 'Velka'): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: 'Archived' }))
  await screen.findByText(title)
  await user.click(more(title))
  await user.click(screen.getByRole('button', { name: `Delete ${title}` }))
  return screen.getByRole('dialog', { name: `Delete ${title}?` })
}

describe('Delete (LIB-18, SEC-40)', () => {
  it('is offered only under Archived, names the document with the exact warning, and focuses the password field', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    expect(screen.queryByRole('button', { name: /^Delete/ })).toBeNull()
    const dialog = await openDelete(user)
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAccessibleDescription("This permanently deletes Velka and its whole history. This can't be undone.")
    const field = within(dialog).getByLabelText('Your password')
    expect(field).toHaveAttribute('type', 'password')
    expect(field).toHaveAttribute('autocomplete', 'current-password')
    expect(field).toHaveFocus()
    expect(server.deleteCalls()).toHaveLength(0)
  })

  it('sends nothing for an empty password, and says so', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Enter your password to delete.')
    expect(server.deleteCalls()).toHaveLength(0)
    expect(within(dialog).getByLabelText('Your password')).toHaveFocus()
  })

  it('a wrong password stays in the dialog, empties and refocuses the field, and nothing is deleted', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), 'wrong one')
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("That password didn't match. Nothing was deleted.")
    expect(within(dialog).getByLabelText('Your password')).toHaveValue('')
    expect(within(dialog).getByLabelText('Your password')).toHaveFocus()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(rowNames()).toContain('Velka')
    expect(deleteBodies(server)).toEqual([{ schema_version: 1, password: 'wrong one' }])
  })

  it('the right password deletes: the password is only in the body, the row goes, focus moves on, and it is announced', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), DELETE_PASSWORD)
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(server.deleteCalls()).toHaveLength(1)
    const call = server.deleteCalls()[0]
    expect(call).toMatchObject({ method: 'POST', url: '/campaigns/cmp_A/documents/doc_v/delete' })
    expect(call.url).not.toContain(DELETE_PASSWORD.split(' ')[0])
    expect(JSON.parse(call.body ?? 'null')).toEqual({ schema_version: 1, password: DELETE_PASSWORD })
    await waitFor(() => expect(rowNames()).toEqual(['Wren']))
    expect(screen.getByRole('button', { name: /^Wren/ })).toHaveFocus()
    expect(status()).toHaveTextContent('Deleted Velka')
    expect(window.location.href).not.toContain('Velka')
    // The password is nowhere in the page once the dialog is gone.
    expect(document.body.innerHTML).not.toContain(DELETE_PASSWORD)
  })

  it('Enter in the field submits', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), `${DELETE_PASSWORD}{Enter}`)
    await waitFor(() => expect(server.deleteCalls()).toHaveLength(1))
  })

  it('Escape and Cancel close it with nothing sent, and focus returns to the row overflow', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    await openDelete(user)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(panelApi.open).toBe(true)
    expect(more('Velka')).toHaveFocus()
    await user.click(more('Velka'))
    await user.click(screen.getByRole('button', { name: 'Delete Velka' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(server.deleteCalls()).toHaveLength(0)
  })

  it('traps Tab inside: from the last control to the first, and Shift+Tab back', async () => {
    const user = userEvent.setup()
    await mountPanel(lifecycle())
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    const field = within(dialog).getByLabelText('Your password')
    act(() => within(dialog).getByRole('button', { name: 'Delete' }).focus())
    await user.tab()
    expect(field).toHaveFocus()
    await user.tab({ shift: true })
    expect(within(dialog).getByRole('button', { name: 'Delete' })).toHaveFocus()
  })

  it('while a delete is in flight the button is aria-disabled, a second press and Escape do nothing', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle({ remove: () => 'defer' }))
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), DELETE_PASSWORD)
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    const busy = await within(dialog).findByRole('button', { name: 'Deleting…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    await user.click(busy)
    await user.keyboard('{Escape}')
    expect(server.deleteCalls()).toHaveLength(1)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it.each([
    ['429', { status: 429, body: {} }, 'Too many changes at once. Wait a moment and try again.'],
    ['503', { status: 503, body: {} }, "Couldn't delete Velka. Nothing was deleted."],
  ])('a %s says so inside the dialog and keeps it open', async (_label, reply, text) => {
    const user = userEvent.setup()
    await mountPanel(lifecycle({ remove: () => reply }))
    await screen.findByText('Ondrey')
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), DELETE_PASSWORD)
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(text)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('a document that is no longer archived is not deleted: the dialog closes, it says so, and the lists reload', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(
      lifecycle({ remove: () => ({ status: 409, body: { detail: { code: 'document_not_archived', message: 'no', retryable: false } } }) }),
    )
    await screen.findByText('Ondrey')
    const before = server.libraryCalls().length
    const dialog = await openDelete(user)
    await user.type(within(dialog).getByLabelText('Your password'), DELETE_PASSWORD)
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(status()).toHaveTextContent("Velka isn't archived any more. Nothing was deleted.")
    await waitFor(() => expect(server.libraryCalls().length).toBeGreaterThan(before + 1))
  })

  it('deleting the document that is open makes the canvas say it is not available', async () => {
    const user = userEvent.setup()
    let deleted = false
    const route: Route = (call) => {
      if (/\/delete$/.test(call.url)) deleted = true
      if (deleted && call.method === 'GET' && /\/documents\/doc_v$/.test(call.url)) return { status: 404, body: {} }
      if (call.method === 'GET' && /\/documents\/doc_v$/.test(call.url)) {
        return { status: 200, body: documentBody('cmp_A', 'doc_v', { archived: true }) }
      }
      return lifecycle()(call)
    }
    await mountPanel(route, 'npcs', true)
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await user.click(await screen.findByRole('button', { name: /^Velka/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    await user.click(more('Velka'))
    await user.click(screen.getByRole('button', { name: 'Delete Velka' }))
    await user.type(within(screen.getByRole('dialog')).getByLabelText('Your password'), DELETE_PASSWORD)
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('unavailable'))
  })
})

// ── The stat block (LIB-12) ──────────────────────────────────────────────────

describe('the stat block dialog (LIB-12)', () => {
  const openStatBlock = async (user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> => {
    await screen.findByText('Tidewarden')
    await user.click(screen.getByRole('button', { name: 'New Stat Block' }))
    return screen.getByRole('dialog', { name: 'New stat block' })
  }
  const fill = async (user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, name: string, ac: string, hp: string) => {
    const set = async (label: string, value: string): Promise<void> => {
      const field = within(dialog).getByLabelText(label)
      await user.clear(field)
      if (value !== '') await user.type(field, value)
    }
    await set('Name', name)
    await set('Armor class', ac)
    await set('Hit points', hp)
  }

  it('focuses the name, and sends nothing while a field is invalid: each refusal sits under its field and the first takes focus', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle(), 'bestiary')
    const dialog = await openStatBlock(user)
    expect(within(dialog).getByLabelText('Name')).toHaveFocus()
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    expect(server.createCalls()).toHaveLength(0)
    const name = within(dialog).getByLabelText('Name')
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAccessibleDescription('Give the stat block a name.')
    expect(within(dialog).getByLabelText('Armor class')).toHaveAccessibleDescription('Armor class is a whole number from 0 to 1,000,000.')
    expect(within(dialog).getByLabelText('Hit points')).toHaveAccessibleDescription('Hit points is a whole number from 0 to 1,000,000.')
    expect(name).toHaveFocus()
  })

  it.each([
    ['letters', 'abc', '12'],
    ['a decimal', '12.5', '12'],
    ['a negative', '-1', '12'],
    ['too big', '1000001', '12'],
  ])('refuses %s as an armor class, with focus on it, and sends nothing', async (_label, ac, hp) => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle(), 'bestiary')
    const dialog = await openStatBlock(user)
    await fill(user, dialog, 'Tidewarden', ac, hp)
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    expect(within(dialog).getByLabelText('Armor class')).toHaveAttribute('aria-invalid', 'true')
    expect(within(dialog).getByLabelText('Armor class')).toHaveFocus()
    expect(within(dialog).getByLabelText('Hit points')).not.toHaveAttribute('aria-invalid')
    expect(server.createCalls()).toHaveLength(0)
  })

  it('creates with name, AC and HP in data, opens the document and closes the dialog; the stat block is stored only now', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle(), 'bestiary')
    const dialog = await openStatBlock(user)
    await fill(user, dialog, '  Deep Warden  ', '16', '110')
    expect(server.createCalls()).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(server.createCalls()).toHaveLength(1))
    expect(JSON.parse(server.createCalls()[0].body ?? '{}')).toMatchObject({
      type: 'statblock', campaign_id: 'cmp_A', data: { name: 'Deep Warden', ac: 16, hp: 110 },
    })
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(status()).toHaveTextContent('Created Deep Warden')
  })

  it('a 503 says nothing was saved, keeps the dialog and the values, and the retry sends the same command id', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const route = createRoute({
      fallback: libraryRoute(ROWS),
      answer: () => (++attempts === 1 ? { status: 503, body: {} } : undefined),
    })
    const { server } = await mountPanel(route, 'bestiary')
    const dialog = await openStatBlock(user)
    await fill(user, dialog, 'Deep Warden', '16', '110')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("Couldn't create the document. Nothing was saved.")
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Deep Warden')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    const ids = server.createCalls().map((call) => (JSON.parse(call.body ?? '{}') as { command_id: string }).command_id)
    expect(ids).toHaveLength(2)
    expect(ids[0]).toBe(ids[1])
  })

  it('changing a field after a failure makes a new command id', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const route = createRoute({
      fallback: libraryRoute(ROWS),
      answer: () => (++attempts === 1 ? { status: 503, body: {} } : undefined),
    })
    const { server } = await mountPanel(route, 'bestiary')
    const dialog = await openStatBlock(user)
    await fill(user, dialog, 'Deep Warden', '16', '110')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await within(dialog).findByRole('alert')
    await fill(user, dialog, 'Deep Warden', '17', '110')
    await user.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(server.createCalls()).toHaveLength(2))
    const ids = server.createCalls().map((call) => (JSON.parse(call.body ?? '{}') as { command_id: string }).command_id)
    expect(ids[0]).not.toBe(ids[1])
  })

  it('Escape and Cancel close it with nothing sent, focus back on New Stat Block, and the panel stays open', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(lifecycle(), 'bestiary')
    await openStatBlock(user)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(panelApi.open).toBe(true)
    expect(screen.getByRole('button', { name: 'New Stat Block' })).toHaveFocus()
    expect(server.createCalls()).toHaveLength(0)
  })
})

// ── LIB-24 ───────────────────────────────────────────────────────────────────

describe('a second tab (LIB-24)', () => {
  const visibility = (state: 'visible' | 'hidden'): void => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(state)
    document.dispatchEvent(new Event('visibilitychange'))
  }

  it('returning to the tab asks for the first page again, quietly: no skeleton, and a row another tab added appears', async () => {
    const rows = { npcs: [...ROWS.npcs] }
    const { server } = await mountPanel(libraryRoute(rows))
    await screen.findByText('Ondrey')
    const before = server.libraryCalls().length
    rows.npcs.unshift({ id: 'doc_n', type: 'npc', title: 'Newcomer' })
    act(() => visibility('visible'))
    expect(screen.queryByText('Loading NPCs…')).toBeNull()
    expect(rowNames()).toContain('Ondrey')
    await waitFor(() => expect(rowNames()).toContain('Newcomer'))
    expect(server.libraryCalls()).toHaveLength(before + 1)
    expect(server.libraryBodies().at(-1)).toEqual(server.libraryBodies()[0])
  })

  it('going hidden asks for nothing, and a failed refresh leaves every row as it was', async () => {
    let failing = false
    const base = libraryRoute(ROWS)
    const { server } = await mountPanel((call) =>
      failing && call.url.endsWith('/library') ? { status: 503, body: {} } : base(call),
    )
    await screen.findByText('Ondrey')
    const before = server.libraryCalls().length
    act(() => visibility('hidden'))
    await flush()
    expect(server.libraryCalls()).toHaveLength(before)
    failing = true
    act(() => visibility('visible'))
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(before + 1))
    await flush()
    expect(rowNames()).toEqual(['Ondrey', 'Brannoch'])
    expect(screen.queryByText("Couldn't load NPCs")).toBeNull()
    expect(within(panel()).getByRole('status')).toBeEmptyDOMElement()
  })

  it('does not ask while the first page is still loading', async () => {
    const base = libraryRoute(ROWS)
    const { server } = await mountPanel((call) => (call.url.endsWith('/library') ? 'defer' : base(call)))
    const before = server.libraryCalls().length
    act(() => visibility('visible'))
    await flush()
    expect(server.libraryCalls()).toHaveLength(before)
    expect(tab('NPCs')).toBeInTheDocument()
  })
})
