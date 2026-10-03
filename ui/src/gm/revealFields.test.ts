/**
 * revealFields.test.ts -- safe field derivation, seeding, mask, effect and summary
 * (agent-forge-harness-1kg.7.3, brief section 8 tests 1 to 10 and the Critic's 2, 5, 6, 7, 10, 11).
 * Each test names the mutation it exists to catch.
 */

import { describe, expect, it } from 'vitest'
import { CharacterSheetLinkSchema, type CharacterSheetLink } from './contracts'
import { REGISTRY, documentTypeById, revealableKeys, type DocumentType, type Registry } from './registry'
import {
  ALIAS_FALLBACK,
  TABLE_ASSETS_SERVED,
  aliasList,
  audienceOfLive,
  defaultAudience,
  liveOf,
  maskFromDraft,
  revealEffect,
  revealRows,
  revealSummary,
  sameAudience,
  seedDraft,
  selectableSeats,
  tickedKeys,
  type DraftAudience,
  type RevealRow,
} from './revealFields'
import { ANA, BRANN, COLE, DEV, liveFixture, pictureFixture, seatFixture } from './revealFixtures'

const NPC = documentTypeById('npc') as DocumentType
const SHEET = documentTypeById('character-sheet') as DocumentType
const NOTES = documentTypeById('session-notes') as DocumentType
const STATBLOCK = documentTypeById('statblock') as DocumentType
const HANDOUT = documentTypeById('handout') as DocumentType

const NPC_DATA = {
  name: 'Ondrey',
  qualifier: 'Harbour almoner',
  tags: ['harbour'],
  voice: 'Quiet, clipped',
  tell: 'Turns the silver pin',
  wants: 'The family signet.',
  leverage: 'Her brother is in the hall.',
  true_identity: 'She signed the ledger.',
}

const TABLE: DraftAudience = { kind: 'table' }
const seats = [BRANN, ANA, COLE]
const ids = (...list: Array<{ participant_id: string }>): DraftAudience => ({
  kind: 'participants',
  ids: list.map((seat) => seat.participant_id),
})

const labels = (rows: readonly RevealRow[]): string[] => rows.map((row) => row.label)
const row = (rows: readonly RevealRow[], label: string): RevealRow => {
  const found = rows.find((candidate) => candidate.label === label)
  if (found === undefined) throw new Error(`no row ${label}`)
  return found
}

describe('revealRows: the allowlist (tests 1 and 2)', () => {
  it('lists the NPC in registry order, folds a group into one row, and never offers true_identity or tags', () => {
    const rows = revealRows(NPC, NPC_DATA)
    expect(labels(rows)).toEqual([
      'Name & voice',
      'Qualifier',
      'Portrait',
      'Tell',
      'Attitude',
      'Wants & leverage',
      'If the party attacks',
      'Notes',
    ])
    const keys = rows.flatMap((r) => r.keys)
    expect(keys).not.toContain('true_identity')
    expect(keys).not.toContain('tags')
    // Mutation: dropping the allowlist filter would add True identity and Tags.
    expect(labels(rows)).not.toContain('True identity')
    expect(labels(rows)).not.toContain('Tags')
  })

  it('excludes a registry-revealable key the contract allowlist lacks (defence in depth)', () => {
    const extra = {
      label: 'Secret extra',
      editable: true,
      required: false,
      revealable: true,
      warning: null,
      bounds: null,
    }
    const stubType: DocumentType = { ...NPC, field_rules: { ...NPC.field_rules, secret_extra: extra } }
    const registryAgrees = revealableKeys(stubType)
    expect(registryAgrees).toContain('secret_extra')
    const rows = revealRows(stubType, { ...NPC_DATA, secret_extra: 'x' })
    // Mutation: removing the intersection with `revealableFields` offers it.
    expect(rows.flatMap((r) => r.keys)).not.toContain('secret_extra')
  })

  it('reads the key set from the registry it is given', () => {
    const none: Registry = { ...REGISTRY, common_field_rules: {} }
    const rows = revealRows(NPC, NPC_DATA, none)
    expect(rows.flatMap((r) => r.keys)).not.toContain('qualifier')
  })

  it('carries the row key list of a group as its allowlisted members, in the group order', () => {
    const rows = revealRows(NPC, NPC_DATA)
    expect(row(rows, 'Name & voice').keys).toEqual(['name', 'voice'])
    expect(row(rows, 'Wants & leverage').keys).toEqual(['wants', 'leverage'])
  })
})

describe('revealRows: warnings come from the registry, once (test 3)', () => {
  it('Wants & leverage carries the warning once, though both members declare it', () => {
    const rows = revealRows(NPC, NPC_DATA)
    // Mutation: hard-coding the string, or not de-duplicating, fails this.
    expect(row(rows, 'Wants & leverage').warnings).toEqual(['Would spoil the lie'])
    expect(row(rows, 'Tell').warnings).toEqual([])
  })

  it('the session-notes Recap row carries the private-thread warning', () => {
    const rows = revealRows(NOTES, { name: 'Session 4', recap: 'We met a ghost.' })
    expect(row(rows, 'Recap').warnings).toEqual(['Summarises your private GM thread'])
  })
})

describe('revealRows: what is selectable (tests 4 and 5)', () => {
  const selectable = (data: Record<string, unknown>, label: string, type = NPC): boolean =>
    row(revealRows(type, data), label).selectable

  it('an empty, whitespace, [] or blank-item value is not selectable, and says Empty', () => {
    expect(selectable({ name: 'A', tell: '' }, 'Tell')).toBe(false)
    expect(selectable({ name: 'A', tell: '   ' }, 'Tell')).toBe(false)
    const rows = revealRows(NPC, { name: 'A', tell: '' })
    expect(row(rows, 'Tell').reason).toBe('empty')
    expect(selectable({ name: 'A', equipment: [] }, 'Equipment', SHEET)).toBe(false)
    expect(selectable({ name: 'A', equipment: ['ok', ' '] }, 'Equipment', SHEET)).toBe(false)
    expect(selectable({ name: 'A', abilities: { str: null } }, 'Ability scores', SHEET)).toBe(false)
  })

  it('0 is selectable', () => {
    expect(selectable({ name: 'A', ac: 0 }, 'Armor Class', SHEET)).toBe(true)
  })

  it('an asset with a value is not selectable while TABLE_ASSETS_SERVED is false, and says so', () => {
    expect(TABLE_ASSETS_SERVED).toBe(false)
    const asset = { asset_id: 'ast_one', media_type: 'image', alt: 'A face', width: 10, height: 10 }
    const rows = revealRows(NPC, { ...NPC_DATA, portrait: asset })
    const portrait = row(rows, 'Portrait')
    // Mutation: flipping the gate would make this selectable.
    expect(portrait.selectable).toBe(false)
    expect(portrait.reason).toBe('asset')
    expect(portrait.preview).toEqual([])
  })

  it('a group is selectable when any member is, and its effective keys are the selectable members only', () => {
    const rows = revealRows(NPC, { name: 'Ondrey', voice: '' })
    const group = row(rows, 'Name & voice')
    expect(group.selectable).toBe(true)
    expect(group.keys).toEqual(['name'])
  })

  it('a group with no selectable member is disabled as empty', () => {
    const rows = revealRows(NPC, { name: 'Ondrey' })
    const group = row(rows, 'Wants & leverage')
    expect(group.selectable).toBe(false)
    expect(group.reason).toBe('empty')
    expect(group.keys).toEqual([])
  })
})

describe('revealRows: the preview is plain text per kind', () => {
  it('writes each kind as text', () => {
    const rows = revealRows(STATBLOCK, {
      name: 'Herald',
      ac: 16,
      abilities: { str: 10, dex: 12 },
      traits: [{ name: 'Amphibious', text: 'Breathes anything.' }],
      languages: 'Common',
    })
    expect(row(rows, 'Armor Class').preview).toEqual([{ key: 'ac', label: 'Armor Class', text: '16' }])
    expect(row(rows, 'Ability scores').preview[0].text).toBe('Strength 10 · Dexterity 12')
    expect(row(rows, 'Traits').preview[0].text).toBe('Amphibious. Breathes anything.')
    const list = revealRows(SHEET, { name: 'A', equipment: ['one', 'two'] })
    expect(row(list, 'Equipment').preview[0].text).toBe('one\ntwo')
  })

  it('a preview holds the field text exactly and never interprets it', () => {
    const rows = revealRows(NPC, { ...NPC_DATA, tell: '<img src=x onerror=alert(1)>' })
    expect(row(rows, 'Tell').preview[0].text).toBe('<img src=x onerror=alert(1)>')
  })
})

describe('seedDraft: REVEAL-4 and owner decision O-2 (test 6)', () => {
  const seed = (
    type: DocumentType,
    data: Record<string, unknown>,
    audience: DraftAudience,
    options: { live?: ReturnType<typeof liveOf>; link?: CharacterSheetLink | null; seatList?: typeof seats } = {},
  ): string[] =>
    [
      ...seedDraft(type, revealRows(type, data), audience, options.live ?? null, options.link ?? null, options.seatList ?? seats),
    ].sort()

  const link = (participant: { participant_id: string }, active = true): CharacterSheetLink =>
    CharacterSheetLinkSchema.parse({
      schema_version: 1,
      document_id: 'doc_sheet',
      participant_id: participant.participant_id,
      seat_active: active,
    })

  it('the NPC table default is name and voice when the portrait is absent (AE-26)', () => {
    expect(seed(NPC, NPC_DATA, TABLE)).toEqual(['name', 'voice'])
  })

  it('a stat block, and session notes, seed nothing (AE-75)', () => {
    expect(seed(STATBLOCK, { name: 'Herald', ac: 16, hp: 10 }, TABLE)).toEqual([])
    expect(seed(NOTES, { name: 'Session 4', recap: 'x' }, TABLE)).toEqual([])
  })

  it('a handout seeds name and body, and drops the portrait it has not got', () => {
    expect(seed(HANDOUT, { name: 'Note', body: 'Meet me' }, TABLE)).toEqual(['body', 'name'])
  })

  it('a character sheet: nothing for the table, the owner default for its linked and confirmed seat, nothing for another seat', () => {
    const data = { name: 'Kestrel', qualifier: 'Rogue', hp: 22, ac: 15, notes: 'Quiet' }
    expect(seed(SHEET, data, TABLE)).toEqual([])
    expect(seed(SHEET, data, ids(BRANN), { link: link(BRANN) })).toEqual(['ac', 'hp', 'name', 'notes', 'qualifier'])
    expect(seed(SHEET, data, ids(ANA), { link: link(BRANN) })).toEqual([])
    // Mutation: ignoring the audience would seed the owner default for ANA.
    expect(seed(SHEET, data, ids(BRANN, ANA), { link: link(BRANN) })).toEqual([])
  })

  it('an NPC revealed to a participant opens empty (O-2)', () => {
    expect(seed(NPC, NPC_DATA, ids(BRANN))).toEqual([])
  })

  it('a sheet linked to a seat that is not confirmed opens on nothing (Critic 5)', () => {
    const offered = seatFixture(BRANN.participant_id, 'Brann', 'offered')
    const data = { name: 'Kestrel', hp: 22 }
    // Mutation: dropping the seat check would seed the owner default for a hidden participant.
    expect(seed(SHEET, data, ids(BRANN), { link: link(BRANN), seatList: [offered, ANA] })).toEqual([])
    expect(seed(SHEET, data, ids(BRANN), { link: link(BRANN, false) })).toEqual([])
  })

  it('a live document to the same audience seeds its live mask; to another audience, the default', () => {
    const picture = pictureFixture({ table: liveFixture('doc_a', ['name', 'qualifier']) })
    const live = liveOf(picture, 'doc_a')
    expect(seed(NPC, NPC_DATA, TABLE, { live })).toEqual(['name', 'qualifier'])
    // Mutation: returning the type default when live, or the live mask for another audience.
    expect(seed(NPC, NPC_DATA, ids(BRANN), { live })).toEqual([])
    expect(seed(NPC, NPC_DATA, TABLE, { live: null })).toEqual(['name', 'voice'])
  })

  it('AE-31: a key present now but absent from the live mask is not ticked', () => {
    const picture = pictureFixture({ table: liveFixture('doc_a', ['name', 'voice']) })
    const live = liveOf(picture, 'doc_a')
    // `tell` is present in the data and was not shown. Mutation: seeding "all present" ticks it.
    expect(seed(NPC, NPC_DATA, TABLE, { live })).toEqual(['name', 'voice'])
  })

  it('seeds a group live with only some members as it is: the row reads ticked and no key is widened or lost', () => {
    const picture = pictureFixture({ table: liveFixture('doc_a', ['name']) })
    const live = liveOf(picture, 'doc_a')
    const rows = revealRows(NPC, NPC_DATA)
    const draft = seedDraft(NPC, rows, TABLE, live, null, seats)
    expect([...draft]).toEqual(['name'])
    expect(tickedKeys(row(rows, 'Name & voice'), draft)).toEqual(['name'])
    expect(tickedKeys(row(rows, 'Qualifier'), draft)).toEqual([])
  })

  it('never seeds the reserved word all, nor a key the rows do not offer', () => {
    const picture = pictureFixture({ table: liveFixture('doc_a', ['name', 'qualifier']) })
    const live = liveOf(picture, 'doc_a')
    const rows = revealRows(NPC, { name: 'Ondrey' })
    const draft = seedDraft(NPC, rows, TABLE, live, null, seats)
    expect([...draft]).toEqual(['name'])
    expect(draft.has('all')).toBe(false)
  })
})

describe('maskFromDraft (test 8)', () => {
  it('lists the selectable draft keys in registry key order, once, and never all', () => {
    const rows = revealRows(NPC, NPC_DATA)
    const mask = maskFromDraft(NPC, rows, new Set(['voice', 'qualifier', 'name', 'name', 'all', 'true_identity', 'tags']))
    expect(mask).toEqual(['name', 'qualifier', 'voice'])
    expect(mask).not.toContain('all')
  })

  it('drops a group member that is empty, so nothing the player cannot see is sent', () => {
    const rows = revealRows(NPC, { name: 'Ondrey', voice: '' })
    // Mutation: sending group keys unfiltered would include `voice`.
    expect(maskFromDraft(NPC, rows, new Set(['name', 'voice']))).toEqual(['name'])
  })

  it('drops a key whose row is not selectable', () => {
    const rows = revealRows(NPC, { name: 'Ondrey' })
    expect(maskFromDraft(NPC, rows, new Set(['name', 'wants', 'leverage', 'tell']))).toEqual(['name'])
  })
})

describe('audiences', () => {
  it('compares a participant list as a set', () => {
    expect(sameAudience(ids(BRANN, ANA), ids(ANA, BRANN))).toBe(true)
    expect(sameAudience(ids(BRANN), ids(ANA))).toBe(false)
    expect(sameAudience(TABLE, TABLE)).toBe(true)
    expect(sameAudience(TABLE, ids(BRANN))).toBe(false)
  })

  it('reads a live audience from the slots holding the document', () => {
    const table = pictureFixture({ table: liveFixture('doc_a', ['name']) })
    expect(audienceOfLive(table, 'doc_a')).toEqual(TABLE)
    expect(audienceOfLive(table, 'doc_b')).toBeNull()
    const disclosure = liveFixture('doc_a', ['name'])
    const copies = pictureFixture({
      participants: { [BRANN.participant_id]: disclosure, [ANA.participant_id]: { ...disclosure }, [COLE.participant_id]: null },
    })
    expect(audienceOfLive(copies, 'doc_a')).toEqual(ids(BRANN, ANA))
  })

  it('reads only the confirmed seats as selectable (Critic 2)', () => {
    const everyStatus = [
      seatFixture('par_open000000000000001', 'Open', 'open'),
      seatFixture('par_offered0000000000001', 'Offered', 'offered'),
      seatFixture('par_notacc00000000000001', 'NotAccepted', 'not_accepted'),
      seatFixture('par_awaiting0000000000001', 'Awaiting', 'awaiting_confirmation'),
      BRANN,
      seatFixture('par_removed0000000000001', 'Removed', 'removed'),
    ]
    // Mutation: admitting awaiting_confirmation gives two.
    expect(selectableSeats(everyStatus).map((seat) => seat.alias)).toEqual(['Brann'])
  })

  it('defaults to the live audience; a live audience holding non-selectable ids never widens to the table (Critic 6)', () => {
    const live = liveFixture('doc_a', ['name'])
    const mixed = pictureFixture({ participants: { [BRANN.participant_id]: live, par_removed000000000001: { ...live } } })
    const result = defaultAudience(NPC, mixed, 'doc_a', null, seats)
    expect(result.audience).toEqual(ids(BRANN))
    expect(result.partial).toBe(true)

    const gone = pictureFixture({ participants: { par_removed000000000001: liveFixture('doc_a', ['name']) } })
    const none = defaultAudience(NPC, gone, 'doc_a', null, seats)
    // Mutation: falling back to the table widens a private display to the room.
    expect(none.audience).toEqual({ kind: 'participants', ids: [] })
    expect(none.partial).toBe(true)

    const whole = defaultAudience(NPC, pictureFixture({ table: live }), 'doc_a', null, seats)
    expect(whole).toEqual({ audience: TABLE, partial: false })
  })

  it('defaults a hidden character sheet to its linked, confirmed seat, and anything else to the table', () => {
    const picture = pictureFixture()
    const link = CharacterSheetLinkSchema.parse({
      schema_version: 1,
      document_id: 'doc_sheet',
      participant_id: BRANN.participant_id,
      seat_active: true,
    })
    expect(defaultAudience(SHEET, picture, 'doc_sheet', link, seats).audience).toEqual(ids(BRANN))
    expect(defaultAudience(NPC, picture, 'doc_a', link, seats).audience).toEqual(TABLE)
    expect(defaultAudience(SHEET, picture, 'doc_sheet', null, seats).audience).toEqual(TABLE)
    const offered = [seatFixture(BRANN.participant_id, 'Brann', 'offered')]
    expect(defaultAudience(SHEET, picture, 'doc_sheet', link, offered).audience).toEqual(TABLE)
  })
})

describe('aliasList', () => {
  it('names one, two, and three or more with a count', () => {
    expect(aliasList([BRANN.participant_id], seats)).toBe('Brann')
    expect(aliasList([BRANN.participant_id, ANA.participant_id], seats)).toBe('Brann and Ana')
    expect(aliasList([BRANN.participant_id, ANA.participant_id, COLE.participant_id], seats)).toBe('Brann, Ana and 1 other')
    expect(aliasList([BRANN.participant_id, ANA.participant_id, COLE.participant_id, DEV.participant_id], [...seats, DEV])).toBe(
      'Brann, Ana and 2 others',
    )
  })

  it('reads an unknown or removed seat as a player', () => {
    expect(aliasList(['par_unknown00000000001'], seats)).toBe(ALIAS_FALLBACK)
  })
})

describe('revealEffect (tests 9 and the Critic 7 and 11)', () => {
  const effect = (
    picture: ReturnType<typeof pictureFixture>,
    audience: DraftAudience,
    mask: string[],
    seatList = seats,
  ) => revealEffect({ picture, documentId: 'doc_a', audience, mask, seats: seatList })
  const hidden = pictureFixture()

  it('a live document with an empty mask stops, and the effect names no second button', () => {
    const live = pictureFixture({ table: liveFixture('doc_a', ['name']) })
    expect(effect(live, TABLE, [])).toMatchObject({ kind: 'stop', label: 'Stop showing' })
  })

  it('a hidden document with an empty mask does nothing and Reveal is disabled', () => {
    expect(effect(hidden, TABLE, [])).toMatchObject({ kind: 'none', label: 'Reveal', notices: [] })
  })

  it('a hidden document to an empty slot reveals, with the audience in the label', () => {
    expect(effect(hidden, TABLE, ['name'])).toMatchObject({ kind: 'reveal', label: 'Reveal to the table', notices: [] })
    expect(effect(hidden, ids(BRANN), ['name'])).toMatchObject({ kind: 'reveal', label: 'Reveal to Brann' })
    expect(effect(hidden, ids(BRANN, ANA), ['name'])).toMatchObject({ kind: 'reveal', label: 'Reveal to 2 players' })
  })

  it('a hidden document into a slot holding another document replaces it, and says so (I-7)', () => {
    const other = pictureFixture({ table: liveFixture('doc_other', ['name']) })
    expect(effect(other, TABLE, ['name'])).toEqual({
      kind: 'replace',
      label: 'Reveal and replace',
      notices: ['This replaces what the table is seeing now.'],
    })
    const privately = pictureFixture({ participants: { [BRANN.participant_id]: liveFixture('doc_other', ['name']) } })
    expect(effect(privately, ids(BRANN, ANA), ['name']).notices).toEqual(['This replaces what Brann is seeing now.'])
    expect(effect(privately, ids(BRANN, ANA), ['name']).kind).toBe('replace')
  })

  it('a live document to the same audience: an unchanged mask is Update, disabled; a changed one is Update', () => {
    const live = pictureFixture({ table: liveFixture('doc_a', ['name', 'voice']) })
    expect(effect(live, TABLE, ['voice', 'name'])).toMatchObject({
      kind: 'none',
      label: 'Update',
      notices: ['Nothing has changed.'],
    })
    // Mutation: swapping the update and move branches, or comparing masks by order.
    expect(effect(live, TABLE, ['name', 'qualifier'])).toMatchObject({ kind: 'update', label: 'Update', notices: [] })
  })

  it('REVEAL-8: the same mask is still an Update when the GM chose a newer version (Use latest version)', () => {
    const live = pictureFixture({ table: liveFixture('doc_a', ['name', 'voice']) })
    const newer = revealEffect({ picture: live, documentId: 'doc_a', audience: TABLE, mask: ['name', 'voice'], seats, versionChanged: true })
    expect(newer).toMatchObject({ kind: 'update', label: 'Update', notices: [] })
    // Mutation: ignoring the flag would leave the button disabled with "Nothing has changed."
    const same = revealEffect({ picture: live, documentId: 'doc_a', audience: TABLE, mask: ['name', 'voice'], seats, versionChanged: false })
    expect(same.kind).toBe('none')
    // It never turns a stop, a reveal or an empty choice into something else.
    expect(revealEffect({ picture: live, documentId: 'doc_a', audience: TABLE, mask: [], seats, versionChanged: true }).kind).toBe('stop')
    expect(revealEffect({ picture: pictureFixture(), documentId: 'doc_a', audience: TABLE, mask: ['name'], seats, versionChanged: true }).kind).toBe('reveal')
  })

  it('a live document to another audience moves, naming only who loses it', () => {
    const live = pictureFixture({ table: liveFixture('doc_a', ['name']) })
    expect(effect(live, ids(BRANN), ['name'])).toMatchObject({
      kind: 'move',
      label: 'Move to Brann',
      notices: ['It stops showing to the table.'],
    })
    const disclosure = liveFixture('doc_a', ['name'])
    const copies = pictureFixture({
      participants: { [BRANN.participant_id]: disclosure, [ANA.participant_id]: { ...disclosure } },
    })
    expect(effect(copies, TABLE, ['name'])).toMatchObject({
      kind: 'move',
      label: 'Move to the table',
      notices: ['It stops showing to Brann and Ana.'],
    })
    // [Brann, Ana] -> [Ana, Cole]: only Brann loses it.
    expect(effect(copies, ids(ANA, COLE), ['name']).notices).toEqual(['It stops showing to Brann.'])
    // [Brann, Ana] -> [Brann, Ana, Cole] widens: nobody loses it, so no notice.
    expect(effect(copies, ids(BRANN, ANA, COLE), ['name'])).toMatchObject({ kind: 'move', label: 'Move to 3 players', notices: [] })
  })

  it('composes notices: a move into a slot holding another document also carries the replace notice', () => {
    const disclosure = liveFixture('doc_a', ['name'])
    const picture = pictureFixture({
      table: disclosure,
    })
    const both = pictureFixture({
      table: disclosure,
      participants: { [BRANN.participant_id]: liveFixture('doc_other', ['name']) },
    })
    expect(effect(picture, ids(BRANN), ['name']).notices).toEqual(['It stops showing to the table.'])
    expect(effect(both, ids(BRANN), ['name'])).toEqual({
      kind: 'move',
      label: 'Move to Brann',
      notices: ['It stops showing to the table.', 'This replaces what Brann is seeing now.'],
    })
  })

  it('Chosen players with nobody ticked is its own state (Critic 7)', () => {
    const empty: DraftAudience = { kind: 'participants', ids: [] }
    expect(effect(hidden, empty, ['name'])).toMatchObject({ kind: 'none', notices: ['Choose at least one player.'] })
    const live = pictureFixture({ table: liveFixture('doc_a', ['name']) })
    expect(effect(live, empty, ['name'])).toMatchObject({ kind: 'none', notices: ['Choose at least one player.'] })
    expect(effect(live, empty, [])).toMatchObject({ kind: 'stop' })
  })

  it('names an unknown seat as a player in a notice', () => {
    const live = pictureFixture({ participants: { par_unknown00000000001: liveFixture('doc_a', ['name']) } })
    expect(effect(live, TABLE, ['name']).notices).toEqual(['It stops showing to a player.'])
  })
})

describe('revealSummary (test 10)', () => {
  const summaryOf = (mask: string[]) => {
    const summary = revealSummary(pictureFixture({ table: liveFixture('doc_a', mask) }), 'doc_a', NPC, seats)
    if (summary === null) throw new Error('not live')
    return summary
  }

  it('folds a group to its label only when every member is masked', () => {
    expect(summaryOf(['name', 'qualifier', 'voice'])).toEqual({
      fields: 'Name & voice, Qualifier',
      audience: 'to the table',
      waiting: false,
    })
    expect(summaryOf(['name']).fields).toBe('Name')
    expect(summaryOf(['voice', 'qualifier']).fields).toBe('Voice, Qualifier')
  })

  it('says A, B, C +n beyond three entries', () => {
    expect(summaryOf(['name', 'qualifier', 'voice', 'tell', 'attitude']).fields).toBe('Name & voice, Qualifier, Tell +1')
    expect(summaryOf(['name', 'qualifier', 'voice', 'tell', 'attitude', 'notes']).fields).toBe('Name & voice, Qualifier, Tell +2')
  })

  it('names the audience, and flags a copy still waiting', () => {
    const live = liveFixture('doc_a', ['name'], { pending_delivery: true })
    const waiting = pictureFixture({ participants: { [BRANN.participant_id]: live } })
    expect(revealSummary(waiting, 'doc_a', NPC, seats)).toEqual({ fields: 'Name', audience: 'to Brann', waiting: true })
    const two = liveFixture('doc_a', ['name'])
    const group = pictureFixture({ participants: { [BRANN.participant_id]: two, [ANA.participant_id]: { ...two } } })
    expect(revealSummary(group, 'doc_a', NPC, seats)?.audience).toBe('to 2 players')
  })
})
