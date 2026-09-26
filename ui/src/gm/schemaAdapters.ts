/**
 * Schema adapters for a document type's field definitions (1kg.5.3) — the
 * client half of `service/workbench_adapters.py`.
 *
 * A type's `type_version` says which revision of its field definitions a
 * stored document's `data` conforms to (`DOC_TYPE_VERSION`). All eight types
 * are at version 1 and nothing is released, so THIS REGISTRY IS EMPTY. What it
 * provides is the frame: when a later bead makes an incompatible change it
 * bumps that type's version and registers the step that carries a document
 * forward.
 *
 * Separate from `adapters.ts`, which is the wire-to-design-system adapter
 * (1kg.1.2) and a different thing entirely, and separate from `contracts.ts`,
 * which the reveal family is editing in parallel.
 *
 * `upgrade` walks one step at a time and REFUSES rather than guesses when a
 * step is missing, because a document half-migrated by a skipped step is worse
 * than one that will not load.
 */

import { DOC_TYPE_VERSION } from './contracts'
import type { DocumentTypeId } from './contracts'

/** One step: the `data` of a document at version n, returned at n + 1. */
export type Adapter = (data: Readonly<Record<string, unknown>>) => Record<string, unknown>

/** No path from the stored version to the current one. The message names the
 * type and the missing step, never the document's content (X-7). */
export class AdapterError extends Error {}

export interface UpgradeResult {
  version: number
  data: Record<string, unknown>
}

/** Requirement 8 (F-11): the range every version this module takes must lie in. */
export const VERSION_MIN = 1
export const VERSION_MAX = 1000

/** A version is an integer from 1 to 1000. Checked explicitly because `NaN`
 * answers `false` to every comparison, so a walk from `NaN` ran no step and
 * reported the document as already current; `Number.isInteger` also refuses
 * `Infinity`, `0.5` and a string. The message names the argument, never the value. */
function aVersion(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < VERSION_MIN || value > VERSION_MAX) {
    throw new Error(`a version starts at 1: ${what} must be an integer from ${VERSION_MIN} to ${VERSION_MAX}`)
  }
  return value
}

/**
 * `(type, from_version) -> adapter`, walked one step at a time.
 *
 * Tests build their own instance with a fixture type, so exercising the frame
 * never registers anything on the one the app uses.
 */
export class AdapterRegistry {
  private readonly steps = new Map<string, Adapter>()

  private static key(type: DocumentTypeId, fromVersion: number): string {
    return `${type}@${fromVersion}`
  }

  /**
   * Record the step from `fromVersion` to `fromVersion + 1`.
   *
   * AN ADAPTER THAT MOVES TEXT FROM ONE KEY TO ANOTHER MUST RESET THE
   * DESTINATION TO `unclassified` (ED-24). A class was granted for the text as
   * it sat under the old key; carrying it across would grant it for text the
   * classifier never saw, which is a widening no transaction covers. The same
   * rule deletes the rows of keys that left the type. The eligibility storage
   * itself is agent-forge-harness-1ir.2.1's — this comment is the contract an
   * adapter author is held to.
   *
   * AN ADAPTER MUST NOT MUTATE NESTED VALUES - IT RECEIVES A SHALLOW COPY, so a
   * list or an object inside `data` is still the caller's.
   */
  register(type: DocumentTypeId, fromVersion: number, adapter: Adapter): void {
    aVersion(fromVersion, 'fromVersion')
    const key = AdapterRegistry.key(type, fromVersion)
    if (this.steps.has(key)) throw new Error(`${type}: a step from version ${fromVersion} is already registered`)
    this.steps.set(key, adapter)
  }

  step(type: DocumentTypeId, fromVersion: number): Adapter | undefined {
    return this.steps.get(AdapterRegistry.key(type, fromVersion))
  }

  /** Whether every step exists, without running any of them. A version that is
   * not a version throws rather than answering. */
  canUpgrade(type: DocumentTypeId, fromVersion: number, to: number = DOC_TYPE_VERSION[type]): boolean {
    aVersion(fromVersion, 'fromVersion')
    aVersion(to, 'to')
    for (let version = fromVersion; version < to; version += 1) {
      if (this.step(type, version) === undefined) return false
    }
    return true
  }

  /**
   * Carry `data` forward to the type's current version, one step at a time.
   * Throws `AdapterError` when a step is missing and `Error` when asked to go
   * backwards or given a version that is not an integer from 1 to 1000 - never
   * a partially-applied result.
   *
   * THE CALLER VALIDATES THE ADAPTED RESULT WITH `check_fields` BEFORE STORING IT
   * (on this side, the request schema that carries it): an adapter is code, and
   * what it returns is not trusted to be a valid document of the new version
   * until the contract says so. AN ADAPTER MUST NOT MUTATE NESTED VALUES - IT
   * RECEIVES A SHALLOW COPY.
   */
  upgrade(
    type: DocumentTypeId,
    fromVersion: number,
    data: Readonly<Record<string, unknown>>,
    to: number = DOC_TYPE_VERSION[type],
  ): UpgradeResult {
    aVersion(fromVersion, 'fromVersion')
    aVersion(to, 'to')
    if (fromVersion > to) {
      throw new Error(`${type}: a document at version ${fromVersion} is newer than ${to}, and cannot be downgraded`)
    }
    // Checked before anything runs, so a half-migrated document is never
    // returned and never written.
    for (let version = fromVersion; version < to; version += 1) {
      if (this.step(type, version) === undefined) {
        throw new AdapterError(`${type}: no adapter from version ${version} to ${version + 1}`)
      }
    }
    let carried: Record<string, unknown> = { ...data }
    for (let version = fromVersion; version < to; version += 1) {
      carried = { ...(this.step(type, version) as Adapter)(carried) }
    }
    return { version: to, data: carried }
  }
}

/** The registry the app uses. Empty: every type is at version 1 and nothing has
 * been released, so no document can need carrying forward yet. */
export const DOC_TYPE_ADAPTERS = new AdapterRegistry()
