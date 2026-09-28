/**
 * Contract-shaped documents, one per type (agent-forge-harness-1kg.6.2).
 *
 * These are wire payloads, not props: `documentFixture` runs each one through
 * `parseDocument`, so a fixture that drifts from the contract fails the suite
 * rather than quietly teaching the renderers a shape the server never sends.
 * `laneFixtures.ts` does the same for the tool lane.
 *
 * The content is deliberately awkward in the places that have broken renderers
 * before: an astral character (a selection counted in UTF-16 units would cut it
 * in half), a bare ampersand (a double-escape would print `&amp;`), an ability
 * block with a missing score (which must not read as 0), a list of one item and
 * a list of many.
 */

import { DOCUMENT_TYPE_IDS, parseDocument, type Document, type DocumentTypeId } from './contracts'
import { documentTypeById } from './registry'
import { clearedValue } from './documentFields'

interface Fixture {
  data: Record<string, unknown>
  qualifier?: string
  summary: string
  changed: readonly string[]
}

function wire(type: DocumentTypeId, fixture: Fixture): Record<string, unknown> {
  return {
    schema_version: 1,
    document_id: `doc_${type.replace(/-/g, '_')}`,
    campaign_id: 'camp_vault_harbour',
    type,
    type_version: 1,
    data: fixture.data,
    write_revision: 7,
    version: {
      number: 3,
      author: 'assistant',
      summary: fixture.summary,
      created_at: '2026-09-16T19:36:00Z',
      sealed: true,
      changed_fields: [...fixture.changed],
      restored_from: null,
    },
    archived: false,
    created_at: '2026-09-14T11:02:00Z',
    updated_at: '2026-09-16T19:36:00Z',
  }
}

const FIXTURES: Record<DocumentTypeId, Fixture> = {
  npc: {
    summary: 'Wants the signet',
    changed: ['wants'],
    data: {
      name: 'Sister Ondrey Vashe',
      qualifier: 'Harbour almoner, and the last witness',
      tags: ['harbour', 'clergy'],
      portrait: { asset_id: 'ast_ondrey', media_type: 'image', alt: 'A grey-robed woman at a harbour rail', width: 416, height: 528 },
      voice: 'Quiet, clipped, never raised',
      tell: 'Turns the silver pin at her collar',
      attitude: 'Wary, and polite about it',
      wants: 'The family signet — proof the drowning of Vault Harbour was ordered by name, not swallowed by accident.',
      leverage: 'Her brother still draws breath in the debtors’ hall, and she has not told anyone.',
      if_attacked: 'She does not fight. She screams for the watch and remembers every face.',
      notes: 'Rolls a \u{1F3B2} for every promise she makes, and keeps the result to herself.',
      true_identity: 'She signed the harbour ledger that night, under another name.',
    },
  },
  statblock: {
    summary: 'Legendary actions added',
    changed: ['legendary_actions'],
    data: {
      name: 'The Long-Winded',
      qualifier: 'Drowned herald of the vault',
      tags: ['undead'],
      ac: 16,
      ac_note: 'barnacle plate',
      hp: 104,
      hit_dice: '11d8 + 33',
      speed: '30 ft., swim 40 ft.',
      size: 'Medium',
      creature_type: 'undead',
      alignment: 'neutral evil',
      // `int` is deliberately absent: an unset score must never read as 0.
      abilities: { str: 18, dex: 12, con: 17, wis: 13, cha: 16 },
      saving_throws: 'Con +6, Wis +4',
      skills: 'Perception +4, Stealth +3',
      damage_immunities: 'poison',
      condition_immunities: 'exhaustion, poisoned',
      senses: 'darkvision 60 ft., passive Perception 14',
      languages: 'Common, Aquan',
      challenge_rating: '6',
      xp: 2300,
      traits: [
        { name: 'Amphibious', text: 'It breathes air and water alike.' },
        { name: 'Ledger-bound', text: 'It cannot attack a creature whose name it has struck through.' },
      ],
      actions: [
        { name: 'Multiattack', text: 'It makes two brine-lash attacks.' },
        { name: 'Brine Lash', text: 'Melee weapon attack, +7 to hit, reach 10 ft. Hit: 12 (2d8 + 3) bludgeoning damage.' },
      ],
      bonus_actions: [{ name: 'Undertow', text: 'It drags a grappled creature 10 feet toward deep water.' }],
      reactions: [{ name: 'Answering Tide', text: 'When struck, it rises and the water rises with it.' }],
      legendary_actions: [{ name: 'Recite a Name', text: 'One creature it can see must succeed on a DC 15 Wisdom save.' }],
    },
  },
  handout: {
    summary: 'Text tightened',
    changed: ['body'],
    data: {
      name: 'The Harbour Writ',
      qualifier: 'Pressed into your hand at the gate',
      tags: ['prop'],
      portrait: { asset_id: 'ast_writ', media_type: 'image', alt: 'A water-stained writ with a broken seal', width: 800, height: 1120 },
      body: 'By order of the Harbour Board, the vault below the third pier is closed to all persons until the tide of the ninth.\n\nSigned under seal, and struck through twice.',
    },
  },
  'session-notes': {
    summary: 'Loose threads recorded',
    changed: ['loose_threads'],
    data: {
      name: 'Session 12 — The Ninth Tide',
      qualifier: 'Three hours, one very wet rogue',
      tags: ['harbour'],
      session: 12,
      date: '2026-09-14',
      present: ['Bess', 'Idris', 'Marlow', 'Quen'],
      recap: 'The party talked their way past the harbour watch, found the writ struck through twice, and left before the tide turned.',
      beats: ['The writ was struck through twice', 'Ondrey would not say who signed it', 'The vault door is keyed to a name'],
      loose_threads: ['Who signed the ledger on the ninth?', 'Marlow still owes the ferryman'],
    },
  },
  'quest-log': {
    summary: 'One thread resolved',
    changed: ['resolved_threads'],
    data: {
      name: 'Vault Harbour',
      qualifier: 'Everything still owed',
      tags: ['campaign'],
      open_threads: [
        { name: 'The struck-through name', text: 'Someone removed a name from the harbour ledger on the ninth.' },
        { name: 'The ferryman’s debt', text: 'Marlow promised a favour he has not named.' },
      ],
      cold_threads: [{ name: 'The lighthouse keeper', text: 'Gone since the spring, and nobody is looking.' }],
      resolved_threads: [{ name: 'The almoner’s brother', text: 'Bought out of the debtors’ hall, quietly.' }],
    },
  },
  'character-sheet': {
    summary: 'Equipment updated',
    changed: ['equipment'],
    data: {
      name: 'Idris Quellen',
      qualifier: 'Level 4 ranger, harbour-born',
      tags: ['player'],
      portrait: { asset_id: 'ast_idris', media_type: 'image', alt: 'A young ranger with a gull feather in her hat', width: 416, height: 528 },
      ac: 15,
      hp: 32,
      speed: '30 ft.',
      abilities: { str: 10, dex: 17, con: 14, int: 11, wis: 15, cha: 8 },
      features: [
        { name: 'Favoured Terrain', text: 'Coast. Advantage on tracking along the tideline.' },
        { name: 'Hunter’s Mark', text: 'Twice per long rest.' },
      ],
      equipment: ['Longbow', 'Shortsword', 'A gull feather she will not explain'],
      notes: 'Will not go below the waterline without a rope in hand.',
    },
  },
  lore: {
    summary: 'History expanded',
    changed: ['history'],
    data: {
      name: 'The Ninth Tide',
      qualifier: 'A harbour custom, and a warning',
      tags: ['custom'],
      region: 'Vault Harbour',
      era: 'Since the Board was founded',
      status: 'Observed, grudgingly',
      summary: 'On the ninth day of each season the harbour closes its vaults and settles its ledgers.',
      history: 'The custom is older than the Board & older than the piers, and nobody will say which drowning began it.',
      rumours: ['The Board keeps a second ledger', 'The tide is early this year'],
    },
  },
  encounter: {
    summary: 'Terrain noted',
    changed: ['terrain'],
    data: {
      name: 'The Third Pier',
      qualifier: 'At the turn of the tide',
      tags: ['combat'],
      difficulty: 'Hard',
      xp_budget: 3400,
      party_level: 4,
      setup: 'The party reaches the pier as the water starts to climb the pilings.',
      combatants: [
        { name: 'The Long-Winded', text: 'Rises from the water on round two.' },
        { name: 'Harbour watch (4)', text: 'They will not help, and they will not leave.' },
      ],
      terrain: 'Wet boards, a ten-foot drop and a rising tide that covers the lowest planks by round four.',
      outcome: 'If the party flees, the herald takes the writ and the vault opens on the ninth.',
    },
  },
}

/** The raw wire payloads, keyed by type. */
export const DOCUMENT_FIXTURES: Readonly<Record<DocumentTypeId, Record<string, unknown>>> = Object.fromEntries(
  DOCUMENT_TYPE_IDS.map((id) => [id, wire(id, FIXTURES[id])]),
) as Record<DocumentTypeId, Record<string, unknown>>

/** Every declared field of a type, cleared the way the wire contract says. */
function emptyData(id: DocumentTypeId, name: string): Record<string, unknown> {
  const type = documentTypeById(id)
  const data: Record<string, unknown> = { name, qualifier: '', tags: [] }
  for (const [key, kind] of Object.entries(type?.fields ?? {})) data[key] = clearedValue(kind)
  return data
}

export interface FixtureOptions {
  /** Every field but the name cleared — the empty-field presentation. */
  empty?: boolean
}

/**
 * One fixture, parsed. It throws rather than returning a placeholder: a fixture
 * the contract refuses is a defect in this file, and a test that renders a
 * placeholder instead would pass for the wrong reason.
 */
export function documentFixture(id: DocumentTypeId, options: FixtureOptions = {}): Document {
  const raw =
    options.empty === true
      ? { ...DOCUMENT_FIXTURES[id], data: emptyData(id, `Untitled ${documentTypeById(id)?.label ?? 'document'}`) }
      : DOCUMENT_FIXTURES[id]
  const parsed = parseDocument(raw)
  if (parsed.kind !== 'ok') throw new Error(`the ${id} fixture is not a valid document: ${parsed.reason}`)
  return parsed.value
}

/**
 * A document as it arrives from a reload (CANVAS-28): its version lists changed
 * fields, and it must still show no gold wash — the wash marks a change that
 * arrived live in THIS client, which is the parent's decision, not the
 * document's.
 */
export function hydratedFixture(): Document {
  return documentFixture('npc')
}
