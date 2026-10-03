/**
 * documentTitle -- what a document is called on the canvas (agent-forge-harness-1kg.6.3).
 *
 * The title of a document is its `data.name` (the one common field every type
 * has, LIB-12). A name that is somehow empty falls back to `Untitled <type label>`
 * so a heading, a loss-guard dialog and a screen reader never read out nothing.
 *
 * A title is GM-private text (X-7): this returns it for RENDERING and
 * announcing. Callers never use it as a key, an id, a storage value or a log field.
 */

import { WORKBENCH_COPY } from '../shell/workbenchCopy'
import type { Document } from './contracts'
import { documentTypeById } from './registry'

export function documentTitle(document: Document): string {
  const name = Object.hasOwn(document.data, 'name') ? document.data.name : undefined
  if (typeof name === 'string' && name.trim() !== '') return name
  return WORKBENCH_COPY.untitled(documentTypeById(document.type)?.label ?? 'document')
}
