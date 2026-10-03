/**
 * revealSnapshot -- the one object a reveal's preview text and version both come from
 * (agent-forge-harness-1kg.7.3; the Critic's items 8 and 10).
 *
 * It is a document's sealed answer (a hidden document, or "Use latest version") or a pinned version
 * (a live one). `snapshotUsable` says whether this bundle may derive rows from it: the type must be
 * one it knows exactly, at the same `type_version`, agreeing with the live entry, sealed, and not
 * archived. Anything else is "this document can't be shown right now" and nothing is sent.
 */

import type { DocumentReadResult, VersionReadResult } from './documentApi'
import type { DocumentType } from './registry'
import type { LiveDocument } from './revealFields'

export interface Snapshot {
  readonly type: string
  readonly typeVersion: number
  readonly data: Readonly<Record<string, unknown>>
  readonly version: number
  readonly sealed: boolean
  readonly archived: boolean
}

export type SnapshotRead =
  | { readonly kind: 'ok'; readonly snapshot: Snapshot }
  | { readonly kind: 'refused' | 'unavailable' | 'failed' | 'unauthorized' }

export function snapshotOfDocument(result: DocumentReadResult): SnapshotRead {
  if (result.kind === 'unsupported') return { kind: 'refused' }
  if (result.kind !== 'ok') return { kind: result.kind }
  const { document } = result
  return {
    kind: 'ok',
    snapshot: {
      type: document.type,
      typeVersion: document.type_version,
      data: document.data,
      version: document.version.number,
      sealed: document.version.sealed,
      archived: document.archived,
    },
  }
}

export function snapshotOfVersion(result: VersionReadResult): SnapshotRead {
  if (result.kind === 'unsupported') return { kind: 'refused' }
  if (result.kind !== 'ok') return { kind: result.kind }
  const { snapshot } = result
  return {
    kind: 'ok',
    snapshot: {
      type: snapshot.type,
      typeVersion: snapshot.type_version,
      data: snapshot.data,
      version: snapshot.version.number,
      sealed: snapshot.version.sealed,
      archived: false,
    },
  }
}

/**
 * Critic 10 (ED-24, X-8): a type this bundle does not know exactly is a guess at an allowlist, so no
 * rows and no Confirm. Pure and exported, so the check that sits behind the schema's own is tested
 * directly (PR-2 F-5).
 */
export function snapshotUsable(
  snapshot: Snapshot,
  type: DocumentType | undefined,
  live: Pick<LiveDocument, 'type'> | null,
): type is DocumentType {
  return (
    type !== undefined &&
    snapshot.typeVersion === type.type_version &&
    (live === null || live.type === snapshot.type) &&
    snapshot.sealed &&
    !snapshot.archived
  )
}
