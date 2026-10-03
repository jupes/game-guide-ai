/**
 * revealSnapshot.test.ts -- the one object a reveal's preview text and version come from, and the
 * rule for when it may be used at all (agent-forge-harness-1kg.7.3; the Critic's items 8 and 10, and
 * PR-2's F-5: the host's own check, behind `DocumentSchema`'s, is tested directly).
 */

import { describe, expect, it } from 'vitest'
import { documentTypeById } from './registry'
import { snapshotUsable, type Snapshot } from './revealSnapshot'

const NPC = documentTypeById('npc')

const SNAPSHOT: Snapshot = {
  type: 'npc',
  typeVersion: NPC?.type_version ?? 1,
  data: { name: 'Ondrey' },
  version: 2,
  sealed: true,
  archived: false,
}

describe('snapshotUsable', () => {
  it('accepts a sealed, live-agreeing snapshot of a type this bundle knows exactly', () => {
    expect(snapshotUsable(SNAPSHOT, NPC, null)).toBe(true)
    expect(snapshotUsable(SNAPSHOT, NPC, { type: 'npc' })).toBe(true)
  })

  it('refuses a type this bundle does not know (X-8)', () => {
    expect(snapshotUsable(SNAPSHOT, undefined, null)).toBe(false)
  })

  it('refuses another era of the type: keys may have changed meaning (ED-24)', () => {
    // Mutation: dropping the version comparison would accept it.
    expect(snapshotUsable({ ...SNAPSHOT, typeVersion: (NPC?.type_version ?? 1) + 1 }, NPC, null)).toBe(false)
    expect(snapshotUsable({ ...SNAPSHOT, typeVersion: 0 }, NPC, null)).toBe(false)
  })

  it('refuses a snapshot whose type disagrees with the live entry', () => {
    expect(snapshotUsable(SNAPSHOT, NPC, { type: 'statblock' })).toBe(false)
  })

  it('refuses a snapshot that is not sealed, or whose document is archived', () => {
    expect(snapshotUsable({ ...SNAPSHOT, sealed: false }, NPC, null)).toBe(false)
    expect(snapshotUsable({ ...SNAPSHOT, archived: true }, NPC, null)).toBe(false)
  })
})
