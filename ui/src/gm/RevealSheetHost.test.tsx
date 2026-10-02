/**
 * RevealSheetHost.test.tsx -- the sheet wired to the real store, over a recording
 * server that can hold an answer back (agent-forge-harness-1kg.7.3, brief tests 19 to 28
 * and the Critic's items 1, 2, 5 to 10, 13, 14). What is proven is what the GM can do and
 * what is SENT: every "nothing is sent" assertion sits beside a positive control.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Seat } from './contracts'
import {
  campaignFixture, defaultWorkbenchRoute, documentBody, flush, live, mountSelected, revealPicture, run, seatBody,
  tableSessionBody, versionBody, type Call, type Route,
} from '../testing/workbenchHarness'
import { CanvasHost } from './CanvasHost'
import { ANA, BRANN, liveFixture, seatFixture } from './revealFixtures'
import { RevealSheetHost } from './RevealSheetHost'

const DOC = 'doc_a'
const SESSION = 'ses_revealFixtureSession00001'
const OTHER_SESSION = 'ses_tableSessionIsNotThePicture01'

type Reply = { status: number; body?: unknown } | 'defer'

interface World {
  /** The body for `GET /reveals`. */
  picture: Record<string, unknown>
  seats: readonly Seat[]
  seal: () => Reply
  version: (number: number) => Reply
  link: Reply
  confirm: (call: Call) => Reply
  stop: Reply
  start: Reply
  read: () => Reply
  /** `GET` of the canvas document itself; omitted, the default NPC. */
  canvas?: () => Reply
  /** The table session's own answer; omitted, none. Its epoch is NOT the picture's, on purpose. */
  session?: unknown
}

const sealedDocument = (version = 7, extra: Record<string, unknown> = {}) => ({
  status: 200,
  body: documentBody('cmp_A', DOC, { version: versionBody(version), ...extra }),
})

const pinned = (number: number, data: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
  status: 200,
  body: {
    schema_version: 1, document_id: DOC, type: 'npc', type_version: 1, version: versionBody(number), data, ...extra,
  },
})

function makeWorld(overrides: Partial<World> = {}): World {
  return {
    picture: revealPicture({ epoch: 3 }),
    seats: [BRANN, ANA],
    seal: () => sealedDocument(),
    version: (number) => pinned(number, { name: 'Pinned name', qualifier: 'Pinned qualifier', voice: 'Pinned voice', wants: 'Pinned wants' }),
    link: { status: 200, body: { schema_version: 1, document_id: DOC, participant_id: null, seat_active: false } },
    confirm: () => ({ status: 500, body: {} }),
    stop: { status: 200, body: revealPicture({ epoch: 4 }) },
    start: { status: 200, body: tableSessionBody({}) },
    read: () => ({ status: 200, body: world.picture }),
    ...overrides,
  }
}

let world = makeWorld()

const isGet = (call: Call, pattern: RegExp): boolean => call.method === 'GET' && pattern.test(call.url)
const isPost = (call: Call, pattern: RegExp): boolean => call.method === 'POST' && pattern.test(call.url)
const REVEALS = /\/reveals$/
const STOP = /\/reveals\/stop$/
const SEAL = /\/seal$/

const route: Route = (call) => {
  const answer = (reply: Reply) => reply
  if (isGet(call, REVEALS)) return answer(world.read())
  if (world.canvas !== undefined && isGet(call, /\/documents\/doc_a$/)) return answer(world.canvas())
  if (isGet(call, /\/table-session$/)) return { status: 200, body: world.session ?? tableSessionBody(null) }
  if (isGet(call, /\/participants/)) return { status: 200, body: seatBody(world.seats) }
  if (isPost(call, SEAL)) return answer(world.seal())
  if (isGet(call, /\/versions\/\d+$/)) return answer(world.version(Number(call.url.split('/').pop())))
  if (isGet(call, /\/link$/)) return answer(world.link)
  if (isPost(call, REVEALS)) return answer(world.confirm(call))
  if (isPost(call, STOP)) return answer(world.stop)
  if (isPost(call, /\/table-session$/)) return answer(world.start)
  return defaultWorkbenchRoute(call)
}

const posts = (server: { calls: Call[] }, pattern: RegExp): Call[] => server.calls.filter((call) => isPost(call, pattern))
const confirmBodies = (server: { calls: Call[] }): Array<Record<string, unknown>> =>
  posts(server, REVEALS).map((call) => JSON.parse(call.body ?? '{}') as Record<string, unknown>)

const dialog = (): HTMLElement => screen.getByRole('dialog')
const inDialog = () => within(dialog())

async function openSheet(options: { worldOverrides?: Partial<World>; documentId?: string } = {}) {
  world = makeWorld(options.worldOverrides)
  const mounted = await mountSelected(
    (server) => (
      <>
        <CanvasHost fetchImpl={server.fetchImpl} />
        <RevealSheetHost fetchImpl={server.fetchImpl} />
      </>
    ),
    { route },
  )
  await run(() => live.actions.openDocument({ documentId: options.documentId ?? DOC, title: null }, { gesture: true }))
  const user = userEvent.setup()
  const button = await screen.findByRole('button', { name: /Reveal to party|Change what the table sees|Update…/ })
  await user.click(button)
  await screen.findByRole('dialog')
  return { ...mounted, user, button }
}

/** The sheet once it has finished preparing. */
const ready = async (): Promise<void> => {
  await waitFor(() => expect(inDialog().queryByText('Getting the document ready…')).toBeNull())
}

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('opening the sheet: preparing, and what it seals and reads (test 20)', () => {
  it('says it is getting the document ready while the seal is held back, with Cancel enabled', async () => {
    const { server } = await openSheet({ worldOverrides: { seal: () => 'defer' } })
    expect(inDialog().getByText('Getting the document ready…')).toBeVisible()
    expect(inDialog().getByRole('button', { name: 'Cancel' })).toBeEnabled()
    expect(inDialog().queryAllByRole('switch')).toEqual([])
    act(() => server.calls.find((call) => isPost(call, SEAL))?.reply(sealedDocument()))
    await ready()
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeInTheDocument()
  })

  it('seals a hidden document first, reads the seats, and sends nothing to /reveals (AE-26)', async () => {
    const { server } = await openSheet()
    await ready()
    expect(posts(server, SEAL)).toHaveLength(1)
    expect(server.calls.some((call) => isGet(call, /\/participants/))).toBe(true)
    // Positive control above; the assertion this test exists for:
    expect(posts(server, REVEALS)).toEqual([])
    expect(server.calls.some((call) => isGet(call, /\/versions\//))).toBe(false)
    expect(dialog()).toHaveAccessibleName('Reveal Ondrey')
  })

  it('offers the rows in registry order with the table default ticked, and never True identity or Tags', async () => {
    await openSheet()
    await ready()
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).not.toBeChecked()
    const wants = inDialog().getByRole('switch', { name: 'Wants & leverage' })
    expect(wants).not.toBeChecked()
    expect(wants).toHaveAccessibleDescription('Would spoil the lie')
    expect(inDialog().queryByRole('switch', { name: /true identity/i })).toBeNull()
    expect(inDialog().queryByRole('switch', { name: 'Tags' })).toBeNull()
    // The portrait has a value but pictures are not served to players yet: shown, disabled, never ticked.
    const portrait = inDialog().getByRole('switch', { name: 'Portrait' })
    expect(portrait).toBeDisabled()
    expect(portrait).toHaveAccessibleDescription("Pictures can't be shown to players yet")
  })

  it('a Confirm sends the SEALED version, the picture\'s session and epoch, keys only, and the table (test 20)', async () => {
    const reply = revealPicture({ epoch: 4, table: liveFixture(DOC, ['name', 'qualifier', 'voice'], { version: 7 }) })
    const { server, user } = await openSheet({ worldOverrides: { confirm: () => ({ status: 200, body: reply }) } })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(1))
    const [body] = confirmBodies(server)
    // Mutation: sending the canvas document's version (3) instead of the sealed one (7).
    expect(body).toMatchObject({
      version: 7, session_id: SESSION, reveal_epoch: 3, audience: { kind: 'table' }, document_id: DOC,
    })
    expect(body.mask).toEqual(['name', 'qualifier', 'voice'])
    expect(JSON.stringify(body)).not.toContain('Harbour almoner')
    expect(JSON.stringify(body)).not.toContain('Quiet, clipped')
    expect(Object.keys(body).sort()).toEqual(
      ['audience', 'command_id', 'document_id', 'mask', 'reveal_epoch', 'schema_version', 'session_id', 'version'],
    )
    // The URLs carry ids only.
    for (const call of server.calls) {
      expect(call.url).not.toContain('Ondrey')
      expect(call.url).not.toContain('Brann')
    }
  })

  it('a live document is NOT sealed: it reads the pinned version, previews that text and sends that version (AE-81 less Use latest)', async () => {
    const picture = revealPicture({ epoch: 3, table: liveFixture(DOC, ['name', 'voice'], { version: 2 }) })
    const reply = revealPicture({ epoch: 4, table: liveFixture(DOC, ['name', 'voice', 'qualifier'], { version: 2 }) })
    const { server, user } = await openSheet({ worldOverrides: { picture, confirm: () => ({ status: 200, body: reply }) } })
    await ready()
    expect(posts(server, SEAL)).toEqual([])
    expect(server.calls.some((call) => isGet(call, /\/versions\/2$/))).toBe(true)
    // The preview is the PINNED text, never the current one.
    expect(inDialog().getByText('Pinned voice')).toBeInTheDocument()
    expect(inDialog().queryByText('Quiet, clipped, never raised')).toBeNull()
    expect(inDialog().getByText(/Revealed · Name & voice · to the table/)).toBeInTheDocument()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    await user.click(inDialog().getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(1))
    expect(confirmBodies(server)[0]).toMatchObject({ version: 2 })
  })

  it('a stale live copy says how to show the latest text', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name'], { version: 2, stale_text: true }) })
    await openSheet({ worldOverrides: { picture } })
    await ready()
    expect(
      inDialog().getByText('The table is seeing an earlier version. To show the latest text, stop showing and reveal it again.'),
    ).toBeInTheDocument()
  })
})

describe('cancelling (test 19, AE-26)', () => {
  it.each([
    ['the Cancel button', async (user: ReturnType<typeof userEvent.setup>) => user.click(inDialog().getByRole('button', { name: 'Cancel' }))],
    ['Escape', async (user: ReturnType<typeof userEvent.setup>) => user.keyboard('{Escape}')],
    ['the scrim', async () => { act(() => { document.querySelector('.gm-reveal__scrim')?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) }) }],
  ])('%s closes the sheet, sends no Confirm, and returns focus to the opener', async (_name, close) => {
    const { server, user, button } = await openSheet()
    await ready()
    expect(inDialog().getByRole('heading', { level: 2 })).toHaveFocus()
    await close(user)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Mutation: a Confirm on close.
    expect(posts(server, REVEALS)).toEqual([])
    expect(button).toHaveFocus()
    expect(live.reveals.sheet).toBeNull()
  })

  it('reopening starts from the seed again: the draft is never remembered (REVEAL-4)', async () => {
    const { user, button } = await openSheet()
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).toBeChecked()
    await user.click(inDialog().getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await user.click(button)
    await ready()
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).not.toBeChecked()
  })
})

describe('the audience (tests 21, Critic 2, 5, 6, 7)', () => {
  it('a participant audience re-seeds the draft to empty and says so; Whole table seeds the default again', async () => {
    const { user } = await openSheet()
    await ready()
    await user.click(inDialog().getByRole('radio', { name: 'Chosen players' }))
    // Chosen players with nobody ticked is its own state.
    expect(inDialog().getByText('Choose at least one player.')).toBeInTheDocument()
    expect(inDialog().getByRole('button', { name: 'Reveal' })).toBeDisabled()
    await user.click(inDialog().getByRole('checkbox', { name: 'Brann' }))
    // Mutation: keeping the old draft would leave Name & voice ticked.
    expect(inDialog().getByText('Choices reset for Brann', { selector: 'p:not([role="status"])' })).toBeInTheDocument()
    expect(within(dialog()).getAllByRole('status').some((node) => node.textContent?.includes('Choices reset for Brann'))).toBe(true)
    for (const toggle of inDialog().getAllByRole('switch')) expect(toggle).not.toBeChecked()
    expect(inDialog().getByRole('button', { name: 'Reveal' })).toBeDisabled()
    await user.click(inDialog().getByRole('radio', { name: 'Whole table' }))
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    expect(inDialog().getByRole('button', { name: 'Reveal to the table' })).toBeEnabled()
  })

  it('offers only confirmed seats: every status gives exactly one checkbox', async () => {
    const seats = [
      seatFixture('par_open000000000000001', 'Open', 'open'),
      seatFixture('par_offered0000000000001', 'Offered', 'offered'),
      seatFixture('par_notacc00000000000001', 'NotAccepted', 'not_accepted'),
      seatFixture('par_awaiting0000000000001', 'Awaiting', 'awaiting_confirmation'),
      BRANN,
      seatFixture('par_removed0000000000001', 'Removed', 'removed'),
    ]
    await openSheet({ worldOverrides: { seats } })
    await ready()
    expect(inDialog().getAllByRole('checkbox').map((box) => box.parentElement?.textContent)).toEqual(['Brann'])
  })

  it('a character sheet linked to a seat that is not confirmed opens on the table with nothing ticked (Critic 5)', async () => {
    const link = { schema_version: 1, document_id: DOC, participant_id: BRANN.participant_id, seat_active: true }
    const offered = seatFixture(BRANN.participant_id, 'Brann', 'offered')
    await openSheet({
      worldOverrides: {
        seats: [offered, ANA], link: { status: 200, body: link }, seal: () => sealedSheet(), canvas: () => sealedSheet(),
      },
    })
    await ready()
    expect(inDialog().getByRole('radio', { name: 'Whole table' })).toBeChecked()
    for (const toggle of inDialog().getAllByRole('switch')) expect(toggle).not.toBeChecked()
  })

  it('a character sheet linked to a confirmed seat opens on that player with the owner default ticked', async () => {
    const link = { schema_version: 1, document_id: DOC, participant_id: BRANN.participant_id, seat_active: true }
    await openSheet({
      worldOverrides: { seats: [BRANN, ANA], link: { status: 200, body: link }, seal: () => sealedSheet(), canvas: () => sealedSheet() },
    })
    await ready()
    expect(inDialog().getByRole('radio', { name: 'Chosen players' })).toBeChecked()
    expect(inDialog().getByRole('checkbox', { name: 'Brann' })).toBeChecked()
    expect(inDialog().getByRole('switch', { name: 'Hit Points' })).toBeChecked()
  })

  it('a document live to a seat the picker cannot offer opens on Chosen players, empty, and never on the table (Critic 6)', async () => {
    const picture = revealPicture({ participants: { par_removed000000000001: liveFixture(DOC, ['name'], { version: 2 }) } })
    await openSheet({ worldOverrides: { picture } })
    await ready()
    // Mutation: falling back to the table would widen a private display to the room.
    expect(inDialog().getByRole('radio', { name: 'Chosen players' })).toBeChecked()
    expect(inDialog().getByRole('radio', { name: 'Whole table' })).not.toBeChecked()
    expect(inDialog().getByText("Some players who can see this can't be chosen here.")).toBeInTheDocument()
    // Nothing is ticked and the document is live: the one action is Stop showing, never a Confirm.
    expect(inDialog().queryByRole('button', { name: /^(Update|Reveal|Move)/ })).toBeNull()
    expect(inDialog().getByRole('button', { name: 'Stop showing' })).toBeEnabled()
  })
})

describe('the effects (test 22)', () => {
  it('a live document with every field unticked is Stop showing, once, and that is the action', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name', 'voice'], { version: 2 }) })
    const { user } = await openSheet({ worldOverrides: { picture } })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Name & voice' }))
    expect(inDialog().getAllByRole('button', { name: 'Stop showing' })).toHaveLength(1)
    expect(inDialog().queryByRole('button', { name: 'Update' })).toBeNull()
  })

  it('moving a live document to a player names who loses it, and the button says Move', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name', 'voice'], { version: 2 }) })
    const { user } = await openSheet({ worldOverrides: { picture } })
    await ready()
    await user.click(inDialog().getByRole('radio', { name: 'Chosen players' }))
    await user.click(inDialog().getByRole('checkbox', { name: 'Brann' }))
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    expect(inDialog().getByRole('button', { name: 'Move to Brann' })).toBeEnabled()
    expect(inDialog().getByText('It stops showing to the table.')).toBeInTheDocument()
  })

  it('revealing into a slot that holds another document says it replaces it, and is labelled for it', async () => {
    const picture = revealPicture({ table: liveFixture('doc_other', ['name'], { version: 2 }) })
    await openSheet({ worldOverrides: { picture } })
    await ready()
    expect(inDialog().getByRole('button', { name: 'Reveal and replace' })).toBeEnabled()
    expect(inDialog().getByText('This replaces what the table is seeing now.')).toBeInTheDocument()
  })
})

describe('a Confirm in flight (test 23, AE-50, Critic 13)', () => {
  it('only Stop showing is enabled, Escape and the scrim are inert, and focus stays on the button', async () => {
    const { server, user } = await openSheet({ worldOverrides: { confirm: () => 'defer' } })
    await ready()
    const reveal = inDialog().getByRole('button', { name: 'Reveal to the table' })
    reveal.focus()
    await user.click(reveal)
    const revealing = await inDialog().findByRole('button', { name: 'Revealing…' })
    // Mutation: a natively `disabled` button would drop focus to <body>.
    expect(revealing).toHaveFocus()
    expect(revealing).toHaveAttribute('aria-disabled', 'true')
    expect(inDialog().getByRole('button', { name: 'Stop showing' })).toBeEnabled()
    expect(inDialog().getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.keyboard('{Escape}')
    act(() => { document.querySelector('.gm-reveal__scrim')?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    // A second press while in flight sends nothing more.
    await user.click(revealing)
    expect(posts(server, REVEALS)).toHaveLength(1)
    // Let the held answer land so nothing is left pending.
    const held = server.calls.find((call) => isPost(call, REVEALS))
    act(() => held?.reply({ status: 500, body: {} }))
    await flush()
  })
})

describe('what a Confirm answers (test 24, Critic 1, 9)', () => {
  it('success closes the sheet, returns focus, and says Shown to the table in the one status node', async () => {
    const reply = revealPicture({ epoch: 4, table: liveFixture(DOC, ['name', 'voice'], { version: 7 }) })
    const { user, button } = await openSheet({ worldOverrides: { confirm: () => ({ status: 200, body: reply }) } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.announcement).toBe('Shown to the table')
    expect(live.reveals.status).toBe('live')
    expect(document.body).toContainElement(button)
  })

  it('names a player, an update and a move in what it announces', async () => {
    const forBrann = revealPicture({ epoch: 4, participants: { [BRANN.participant_id]: liveFixture(DOC, ['name', 'voice'], { version: 7 }) } })
    const { user } = await openSheet({ worldOverrides: { confirm: () => ({ status: 200, body: forBrann }) } })
    await ready()
    await user.click(inDialog().getByRole('radio', { name: 'Chosen players' }))
    await user.click(inDialog().getByRole('checkbox', { name: 'Brann' }))
    await user.click(inDialog().getByRole('switch', { name: 'Name & voice' }))
    await user.click(inDialog().getByRole('button', { name: 'Reveal to Brann' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.announcement).toBe('Shown to Brann')
  })

  it('a 200 whose picture does not hold what was asked is not success: it takes the REVEAL-15 path, and announces nothing (Critic 9)', async () => {
    // A replay after an intervening Stop answers 200 with the document not live.
    const { server, user } = await openSheet({ worldOverrides: { confirm: () => ({ status: 200, body: revealPicture({ epoch: 5 }) }) } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(inDialog().getAllByText('Reveal changed — check and confirm again').length).toBeGreaterThan(0))
    // Mutation: announcing on `ok`.
    expect(live.reveals.announcement).toBe('')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(posts(server, REVEALS)).toHaveLength(1)
  })

  it('a 409 keeps the draft, shows the conflict line, re-reads the picture, and never resends by itself', async () => {
    let epoch = 3
    const { server, user } = await openSheet({
      worldOverrides: {
        confirm: () => ({ status: 409, body: { detail: { code: 'conflict', message: 'x', retryable: false } } }),
        read: () => ({ status: 200, body: revealPicture({ epoch }) }),
      },
    })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    epoch = 4
    const readsBefore = server.calls.filter((call) => isGet(call, REVEALS)).length
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(inDialog().getAllByText('Reveal changed — check and confirm again').length).toBeGreaterThan(0))
    await ready()
    // The picture was read again, and the draft is kept.
    expect(server.calls.filter((call) => isGet(call, REVEALS)).length).toBeGreaterThan(readsBefore)
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).toBeChecked()
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    // Mutation: an automatic retry would make this 2.
    expect(posts(server, REVEALS)).toHaveLength(1)
    // The next press carries the NEW epoch and a NEW command id (Critic 8).
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(2))
    const [first, second] = confirmBodies(server)
    expect(second.reveal_epoch).toBe(4)
    expect(second.command_id).not.toBe(first.command_id)
  })

  it('a refused mask unticks the key that is gone and keeps the rest', async () => {
    const detail = { code: 'validation', message: 'x', retryable: false, field: 'mask', keys: ['voice'] }
    const { user, server } = await openSheet({ worldOverrides: { confirm: () => ({ status: 422, body: { detail } }) } })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    // The document changed under us: voice is gone from the re-sealed text.
    world.seal = () => sealedDocument(8, { data: { name: 'Ondrey', qualifier: 'Harbour almoner', voice: '' } })
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(inDialog().getAllByText('Reveal changed — check and confirm again').length).toBeGreaterThan(0))
    await ready()
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).toBeChecked()
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    // `name` stays; `voice` is empty now, so the row's effective keys no longer include it.
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(2))
    expect(confirmBodies(server)[1].mask).toEqual(['name', 'qualifier'])
    expect(confirmBodies(server)[1].version).toBe(8)
  })

  it('a refused version seals again, a refused audience reads the seats again', async () => {
    const detail = (field: string) => ({ status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false, field } } })
    const { user, server } = await openSheet({ worldOverrides: { confirm: () => detail('version') } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(posts(server, SEAL)).toHaveLength(2))
    await ready()
    const seatsBefore = server.calls.filter((call) => isGet(call, /\/participants/)).length
    world.confirm = () => detail('audience')
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(server.calls.filter((call) => isGet(call, /\/participants/)).length).toBeGreaterThan(seatsBefore))
    // Neither is ever resent by itself.
    expect(posts(server, REVEALS)).toHaveLength(2)
  })

  it('a refused audience whose re-read drops a chosen seat unticks it, disables Reveal, and no later Confirm carries his id (Critic 7)', async () => {
    const detail = { status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false, field: 'audience' } } }
    const { user, server } = await openSheet({ worldOverrides: { confirm: () => detail } })
    await ready()
    await user.click(inDialog().getByRole('radio', { name: 'Chosen players' }))
    await user.click(inDialog().getByRole('checkbox', { name: 'Brann' }))
    await user.click(inDialog().getByRole('switch', { name: 'Name & voice' }))
    expect(inDialog().getByRole('button', { name: 'Reveal to Brann' })).toBeEnabled()
    // Brann's seat is removed before the Confirm lands; the refusal makes the sheet read the seats again.
    world.seats = [ANA]
    const seatsBefore = server.calls.filter((call) => isGet(call, /\/participants/)).length
    await user.click(inDialog().getByRole('button', { name: 'Reveal to Brann' }))
    await waitFor(() => expect(server.calls.filter((call) => isGet(call, /\/participants/)).length).toBeGreaterThan(seatsBefore))
    await waitFor(() => expect(inDialog().getByText('Choose at least one player.')).toBeInTheDocument())
    expect(posts(server, REVEALS)).toHaveLength(1)
    expect(confirmBodies(server)[0].audience).toEqual({ kind: 'participants', participant_ids: [BRANN.participant_id] })
    // Mutation: keeping the stored audience would leave his id behind with the button live.
    expect(inDialog().queryByRole('checkbox', { name: 'Brann' })).toBeNull()
    expect(inDialog().getByRole('checkbox', { name: 'Ana' })).not.toBeChecked()
    expect(inDialog().getByRole('button', { name: 'Reveal' })).toBeDisabled()
    // Positive control: ticking the seat that remains makes the next Confirm carry that seat and never Brann's.
    world.confirm = () => ({ status: 200, body: revealPicture({ epoch: 4, participants: { [ANA.participant_id]: liveFixture(DOC, ['name', 'voice'], { version: 7 }) } }) })
    await user.click(inDialog().getByRole('checkbox', { name: 'Ana' }))
    await user.click(inDialog().getByRole('switch', { name: 'Name & voice' }))
    await user.click(inDialog().getByRole('button', { name: 'Reveal to Ana' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(2))
    expect(confirmBodies(server)[1].audience).toEqual({ kind: 'participants', participant_ids: [ANA.participant_id] })
    expect(JSON.stringify(confirmBodies(server)[1])).not.toContain(BRANN.participant_id)
  })

  it('a 422 with no field is REVEAL-15 too: re-read, keep the draft, ask again', async () => {
    const { user, server } = await openSheet({
      worldOverrides: { confirm: () => ({ status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false } } }) },
    })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(inDialog().getAllByText('Reveal changed — check and confirm again').length).toBeGreaterThan(0))
    expect(posts(server, REVEALS)).toHaveLength(1)
  })

  it("a refused document says it can't be shown, with only Cancel; a 404 says it isn't available", async () => {
    const refused = { status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false, field: 'document_id' } } }
    const { user } = await openSheet({ worldOverrides: { confirm: () => refused } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    expect(await inDialog().findByText("This document can't be shown right now.")).toBeInTheDocument()
    expect(inDialog().getAllByRole('button').map((b) => b.textContent)).toEqual(['Cancel'])
  })

  it('a 404 answer says the document is not available', async () => {
    const { user } = await openSheet({ worldOverrides: { confirm: () => ({ status: 404, body: {} }) } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    expect(await inDialog().findByText("This document isn't available.")).toBeInTheDocument()
    expect(inDialog().getAllByRole('button').map((b) => b.textContent)).toEqual(['Cancel'])
  })

  it('a 429 keeps the draft and says how long to wait', async () => {
    const body = { detail: { code: 'throttled', message: 'x', retryable: true, retry_after_s: 12 } }
    const { user } = await openSheet({ worldOverrides: { confirm: () => ({ status: 429, body }) } })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    expect(await inDialog().findByText('Too many changes at once. Try again in 12 seconds.')).toBeInTheDocument()
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).toBeChecked()
  })

  it('a failure offers Try again, which resends with the SAME command id', async () => {
    const { user, server } = await openSheet({ worldOverrides: { confirm: () => ({ status: 503, body: {} }) } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    expect(await inDialog().findByText("Couldn't reveal — Try again")).toBeInTheDocument()
    expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeChecked()
    // The courtesy: the picture is not re-read and nothing is auto-retried.
    expect(posts(server, REVEALS)).toHaveLength(1)
    await user.click(inDialog().getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(2))
    const [first, second] = confirmBodies(server)
    // Mutation: minting a new id on Try again.
    expect(second.command_id).toBe(first.command_id)
  })
})

describe('a Stop (X-3, REVEAL-22)', () => {
  it('Stop showing from inside the sheet is sent at once, closes the sheet, and is never confirmed by a dialog', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name'], { version: 2 }) })
    const { server, user } = await openSheet({ worldOverrides: { picture } })
    await ready()
    await user.click(inDialog().getByRole('button', { name: 'Stop showing' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(posts(server, STOP)).toHaveLength(1)
    expect(JSON.parse(posts(server, STOP)[0].body ?? '{}')).toMatchObject({ scope: 'document', document_id: DOC })
  })

  it('while a Stop is unacknowledged Confirm is disabled with the reason, and sends nothing', async () => {
    const picture = revealPicture({ table: liveFixture('doc_other', ['name'], { version: 2 }) })
    const { server } = await openSheet({ worldOverrides: { picture, stop: 'defer' } })
    await ready()
    act(() => live.reveals.stop('doc_other', 'Other'))
    await flush()
    expect(inDialog().getByText('Waiting for Stop showing to finish…')).toBeInTheDocument()
    expect(inDialog().getByRole('button', { name: 'Reveal and replace' })).toBeDisabled()
    expect(posts(server, REVEALS)).toEqual([])
    act(() => server.calls.find((call) => isPost(call, STOP))?.reply({ status: 200, body: revealPicture({ epoch: 9 }) }))
    await flush()
  })
})

describe('no live session (REVEAL-1, Critic 17)', () => {
  const noSession = { picture: revealPicture(null) }

  it('says so, starts a session once per press, and re-reads on success', async () => {
    const { server, user } = await openSheet({ worldOverrides: noSession })
    expect(await screen.findByRole('dialog', { name: 'Start a table session' })).toBeInTheDocument()
    expect(inDialog().getByText('Revealing needs a live table session. Starting one shows players nothing.')).toBeInTheDocument()
    expect(inDialog().queryByText(/link|QR|token/i)).toBeNull()
    world.picture = revealPicture({ epoch: 3 })
    await user.click(inDialog().getByRole('button', { name: 'Start session' }))
    await waitFor(() => expect(posts(server, /\/table-session$/)).toHaveLength(1))
    // The picture is re-read and the sheet goes on to prepare.
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Reveal Ondrey' })).toBeInTheDocument())
  })

  it.each([
    ['a session live in another campaign', { status: 409, body: { detail: { code: 'live_elsewhere', message: 'x', retryable: false } } }, 'A session is live in another campaign. End it there first.', false],
    ['an account that cannot start one', { status: 403, body: { detail: { code: 'plan_required', message: 'x', retryable: false } } }, "You can't start a table session on this account.", false],
    ['too many tries', { status: 429, body: { detail: { code: 'throttled', message: 'x', retryable: true } } }, 'Too many tries. Try again in a moment.', true],
    ['a failure', { status: 503, body: {} }, "Couldn't start a session.", true],
  ])('%s is said in words', async (_label, start, text, retry) => {
    const { user } = await openSheet({ worldOverrides: { ...noSession, start } })
    await screen.findByRole('dialog', { name: 'Start a table session' })
    await user.click(inDialog().getByRole('button', { name: 'Start session' }))
    expect(await inDialog().findByText(text)).toBeInTheDocument()
    expect(Boolean(inDialog().queryByRole('button', { name: 'Retry' }))).toBe(retry)
  })
})

describe('unknown and unreadable (REVEAL-13, REVEAL-22)', () => {
  it('an unreadable picture offers no Open control, and a sheet opened anyway offers only Cancel and Stop showing', async () => {
    world = makeWorld({ read: () => ({ status: 503, body: {} }) })
    const { server } = await mountSelected(
      (srv) => (
        <>
          <CanvasHost fetchImpl={srv.fetchImpl} />
          <RevealSheetHost fetchImpl={srv.fetchImpl} />
        </>
      ),
      { route },
    )
    await run(() => live.actions.openDocument({ documentId: DOC, title: null }, { gesture: true }))
    await waitFor(() => expect(screen.getAllByText('Reveal state unknown — reconnecting').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: 'Reveal to party' })).toBeNull()
    act(() => live.reveals.openSheet(DOC))
    expect(await screen.findByRole('dialog', { name: 'Reveal Ondrey' })).toBeInTheDocument()
    expect(inDialog().getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    expect(inDialog().getAllByRole('button').map((button) => button.textContent)).toEqual(['Cancel', 'Stop showing'])
    expect(posts(server, REVEALS)).toEqual([])
  })
})

describe('a type this bundle does not know exactly yields no rows and no Confirm (Critic 10)', () => {
  it.each([
    ['an unknown type', () => ({ status: 200, body: documentBody('cmp_A', DOC, { type: 'dragon-hoard', version: versionBody(7) }) })],
    ['a newer type version', () => ({ status: 200, body: documentBody('cmp_A', DOC, { type_version: 99, version: versionBody(7) }) })],
    ['an unsealed snapshot', () => ({ status: 200, body: documentBody('cmp_A', DOC, { version: { ...versionBody(7), sealed: false } }) })],
    ['an archived document', () => ({ status: 200, body: documentBody('cmp_A', DOC, { archived: true, version: versionBody(7) }) })],
  ])('%s', async (_label, seal) => {
    const { server } = await openSheet({ worldOverrides: { seal } })
    expect(await inDialog().findByText("This document can't be shown right now.")).toBeInTheDocument()
    expect(inDialog().queryAllByRole('switch')).toEqual([])
    expect(inDialog().getAllByRole('button').map((button) => button.textContent)).toEqual(['Cancel'])
    expect(posts(server, REVEALS)).toEqual([])
  })

  it('a pinned snapshot of a newer type version is refused', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name'], { version: 2 }) })
    const { server } = await openSheet({ worldOverrides: { picture, version: () => pinned(2, { name: 'x' }, { type_version: 5 }) } })
    expect(await inDialog().findByText("This document can't be shown right now.")).toBeInTheDocument()
    expect(inDialog().queryAllByRole('switch')).toEqual([])
    expect(posts(server, REVEALS)).toEqual([])
  })

  it('a pinned snapshot of another type than the live one is refused', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name'], { version: 2 }) })
    const { server } = await openSheet({ worldOverrides: { picture, version: () => pinned(2, { name: 'x' }, { type: 'handout' }) } })
    expect(await inDialog().findByText("This document can't be shown right now.")).toBeInTheDocument()
    expect(inDialog().queryAllByRole('switch')).toEqual([])
    expect(posts(server, REVEALS)).toEqual([])
  })

  it('a seal that cannot reach the library says nothing was lost and offers Try again', async () => {
    let up = false
    const { user, server } = await openSheet({ worldOverrides: { seal: () => (up ? sealedDocument() : { status: 503, body: {} }) } })
    expect(await inDialog().findByText("Aetheril can't reach its library right now. Nothing was lost.")).toBeInTheDocument()
    up = true
    await user.click(inDialog().getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(inDialog().getByRole('switch', { name: 'Name & voice' })).toBeInTheDocument())
    expect(posts(server, SEAL)).toHaveLength(2)
  })
})

describe('the picture moves under an open sheet (Critic 8)', () => {
  it('adopts a new epoch, keeps the draft, says so, and the next Confirm carries it', async () => {
    const { server, user } = await openSheet({
      worldOverrides: {
        // The table session reports a different session and epoch than the picture: the Confirm must carry the PICTURE's.
        session: tableSessionBody({ session_id: OTHER_SESSION, reveal_epoch: 99 }),
        confirm: () => ({ status: 200, body: revealPicture({ epoch: 6, table: liveFixture(DOC, ['name', 'voice'], { version: 7 }) }) }),
      },
    })
    await ready()
    await user.click(inDialog().getByRole('switch', { name: 'Qualifier' }))
    world.picture = revealPicture({ epoch: 5 })
    await run(() => live.reveals.refresh())
    await waitFor(() => expect(inDialog().getAllByText('Reveal changed — check and confirm again').length).toBeGreaterThan(0))
    expect(inDialog().getByRole('switch', { name: 'Qualifier' })).toBeChecked()
    await user.click(inDialog().getByRole('button', { name: 'Reveal to the table' }))
    await waitFor(() => expect(posts(server, REVEALS)).toHaveLength(1))
    // Mutation: taking the epoch from the table session would send the stale 3.
    expect(confirmBodies(server)[0].reveal_epoch).toBe(5)
    // Mutation: taking the session from the table session would send OTHER_SESSION.
    expect(confirmBodies(server)[0].session_id).toBe(SESSION)
    expect(confirmBodies(server)[0].session_id).not.toBe(OTHER_SESSION)
  })
})

describe('the sheet cannot strand the shell (Critic 14)', () => {
  it('closes when the canvas document changes or closes, leaving no dialog and no stale sheet', async () => {
    const { user } = await openSheet()
    await ready()
    await run(() => live.actions.openDocument({ documentId: 'doc_b', title: null }, { gesture: false }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.sheet).toBeNull()
    void user
  })

  it('closes when the canvas closes', async () => {
    await openSheet()
    await ready()
    await run(() => live.actions.closeDocument())
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.sheet).toBeNull()
  })

  it('closes on a campaign switch', async () => {
    await openSheet()
    await ready()
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.sheet).toBeNull()
  })
})

// ── helpers for the character-sheet cases ──

function sheetDocFor(): Record<string, unknown> {
  return {
    data: { name: 'Kestrel', qualifier: 'Rogue', hp: 22, ac: 15, notes: 'Quiet' },
    type: 'character-sheet',
    type_version: 1,
    version: versionBody(7),
  }
}

function sealedSheet(): { status: number; body: unknown } {
  return { status: 200, body: documentBody('cmp_A', DOC, sheetDocFor()) }
}

