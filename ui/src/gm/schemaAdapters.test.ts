/**
 * The schema-adapter frame (1kg.5.3) — the client half, mirroring
 * `service/tests/test_workbench_adapters.py`.
 *
 * No real type has a version 2 yet, so the walk is exercised with a FIXTURE
 * type on a registry the tests build themselves. That the shipped one stays
 * empty is itself one of the assertions below.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { AdapterError, AdapterRegistry, DOC_TYPE_ADAPTERS } from './schemaAdapters'
import type { Adapter } from './schemaAdapters'
import { DOCUMENT_TYPE_IDS, DOC_TYPE_VERSION } from './contracts'

// Requirement 7 asks for a fixture type. DocumentTypeId is a closed
// vocabulary, so a synthetic id cannot be minted without changing the
// contract; a real id stands in on a registry these tests own, and the
// target version is passed explicitly. The shipped registry is untouched,
// which the first test asserts.
const FIXTURE_TYPE = 'npc'

/** A step that moves text from one key to another — the case ED-24 is about. */
function rename(oldKey: string, newKey: string): Adapter {
  return (data) => {
    const carried = { ...data }
    if (Object.hasOwn(carried, oldKey)) {
      carried[newKey] = carried[oldKey]
      delete carried[oldKey]
    }
    return carried
  }
}

describe('the schema-adapter frame', () => {
  it('ships empty, because every type is at version 1', () => {
    expect(new Set(Object.values(DOC_TYPE_VERSION))).toEqual(new Set([1]))
    for (const type of DOCUMENT_TYPE_IDS) {
      expect(DOC_TYPE_ADAPTERS.step(type, 1)).toBeUndefined()
    }
    // Nothing to do, so the walk is a no-op that still answers.
    expect(DOC_TYPE_ADAPTERS.upgrade(FIXTURE_TYPE, 1, { name: 'x' })).toEqual({ version: 1, data: { name: 'x' } })
  })

  it('walks a document one step at a time to the target version', () => {
    const registry = new AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, rename('motives', 'wants'))
    registry.register(FIXTURE_TYPE, 2, rename('wants', 'wants_now'))

    expect(registry.upgrade(FIXTURE_TYPE, 1, { name: 'Ondrey', motives: 'The signet.' }, 3)).toEqual({
      version: 3,
      data: { name: 'Ondrey', wants_now: 'The signet.' },
    })
    // Already at the target: returned unchanged, not re-run.
    expect(registry.upgrade(FIXTURE_TYPE, 3, { name: 'Ondrey' }, 3)).toEqual({ version: 3, data: { name: 'Ondrey' } })
    // A partial walk stops where it is asked to.
    expect(registry.upgrade(FIXTURE_TYPE, 1, { motives: 'x' }, 2)).toEqual({ version: 2, data: { wants: 'x' } })
  })

  it('reports a missing step before anything runs', () => {
    const registry = new AdapterRegistry()
    const ran: string[] = []
    registry.register(FIXTURE_TYPE, 1, (data) => {
      ran.push('step 1')
      return { ...data }
    })
    expect(registry.canUpgrade(FIXTURE_TYPE, 1, 2)).toBe(true)
    expect(registry.canUpgrade(FIXTURE_TYPE, 1, 3)).toBe(false)
    expect(() => registry.upgrade(FIXTURE_TYPE, 1, { name: 'x' }, 3)).toThrow(AdapterError)
    expect(() => registry.upgrade(FIXTURE_TYPE, 1, { name: 'x' }, 3)).toThrow('no adapter from version 2 to 3')
    expect(ran).toEqual([])
  })

  it('names the step and never the document in its message (X-7)', () => {
    const registry = new AdapterRegistry()
    const secret = 'Drown the harbourmaster.'
    try {
      registry.upgrade(FIXTURE_TYPE, 1, { name: 'x', notes: secret }, 2)
      expect.unreachable('the walk should have refused')
    } catch (error) {
      const message = (error as Error).message
      expect(message).not.toContain(secret)
      expect(message).not.toContain('notes')
    }
  })

  it('refuses a newer document rather than downgrading it', () => {
    const registry = new AdapterRegistry()
    expect(() => registry.upgrade(FIXTURE_TYPE, 3, { name: 'x' }, 1)).toThrow('cannot be downgraded')
  })

  it('refuses a step registered twice, or from a version that does not exist', () => {
    const registry = new AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, rename('a', 'b'))
    expect(() => registry.register(FIXTURE_TYPE, 1, rename('a', 'c'))).toThrow('already registered')
    expect(() => registry.register(FIXTURE_TYPE, 0, rename('a', 'b'))).toThrow('a version starts at 1')
  })

  it("carries ED-24's reset-to-unclassified rule where an adapter author will read it", () => {
    // The rule has no code to enforce here — eligibility storage is
    // agent-forge-harness-1ir.2.1's — so the contract lives in the JSDoc on
    // `register`, and this test is what keeps it there. Mirrors
    // test_the_register_docstring_carries_ED24s_rule on the Python side.
    const here = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(join(here, 'schemaAdapters.ts'), 'utf-8')
    const body = source.slice(source.indexOf('class AdapterRegistry'))
    const declaration = body.indexOf('\n  register(')
    expect(declaration).toBeGreaterThan(-1)
    const jsdoc = body.slice(body.lastIndexOf('/**', declaration), declaration)
    expect(jsdoc).toContain('unclassified')
    expect(jsdoc).toContain('ED-24')
  })

  it('keeps one type’s steps out of another’s', () => {
    const registry = new AdapterRegistry()
    registry.register('npc', 1, rename('a', 'b'))
    expect(registry.step('npc', 1)).toBeDefined()
    expect(registry.step('lore', 1)).toBeUndefined()
  })
})

describe('a version that is not a version (F-11, requirement 8, 1kg.5.7.2)', () => {
  it.each([
    ['NaN', Number.NaN],
    ['0.5', 0.5],
    ['-1', -1],
    ['0', 0],
    ['1001', 1001],
    ['a string', '1'],
    ['Infinity', Number.POSITIVE_INFINITY],
  ])('refuses %s wherever a version is taken (AC 19)', (_name, given) => {
    // `NaN` answers false to every comparison, so before this a walk from it ran
    // no step and reported the document as already current.
    const version = given as number
    const registry = new AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, rename('a', 'b'))
    const calls = [
      () => registry.upgrade(FIXTURE_TYPE, version, { name: 'x' }, 2),
      () => registry.upgrade(FIXTURE_TYPE, 1, { name: 'x' }, version),
      () => registry.canUpgrade(FIXTURE_TYPE, version, 2),
      () => registry.canUpgrade(FIXTURE_TYPE, 1, version),
      () => registry.register('lore', version, rename('a', 'b')),
    ]
    for (const call of calls) {
      expect(call).toThrow('must be an integer from 1 to 1000')
      expect(call).not.toThrow(AdapterError)
    }
  })

  it('says what the caller and an adapter must do, where an adapter author reads it', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const source = readFileSync(join(here, 'schemaAdapters.ts'), 'utf-8')
    const declaration = source.indexOf('\n  upgrade(')
    const jsdoc = source.slice(source.lastIndexOf('/**', declaration), declaration).replace(/\s+\*?\s*/g, ' ')
    expect(jsdoc).toContain('THE CALLER VALIDATES THE ADAPTED RESULT WITH `check_fields` BEFORE STORING IT')
    expect(jsdoc).toContain('AN ADAPTER MUST NOT MUTATE NESTED VALUES - IT RECEIVES A SHALLOW COPY.')
  })
})
