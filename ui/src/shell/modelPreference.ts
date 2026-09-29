/**
 * What a conversation's model preference IS on the wire (b8o.2, D-9,
 * agent-forge-harness-bta): the one rule both ModelPicker (what it shows) and
 * ChatPane (what it posts to /chat) read, so the two can never disagree.
 */

import type { Conversation } from './conversationStore'

export interface ModelCatalogEntry {
  id: string
  display_name: string
  tier?: string
  supports_attachments?: boolean
  description?: string
}

export interface ModelCatalog {
  default: string
  models: ModelCatalogEntry[]
}

/** The wire default: what the server binds for an omitted model_preference. */
export const AUTO_PREFERENCE = 'auto'

/** What the picker shows until /models answers, or when it cannot. It lists
 * 'auto' alone: an alias list here would name the models in the bundle. Held
 * by identity, so "the served catalog" is exactly `catalog !== FALLBACK_CATALOG`. */
export const FALLBACK_CATALOG: ModelCatalog = {
  default: AUTO_PREFERENCE,
  models: [{ id: AUTO_PREFERENCE, display_name: 'Automatic' }],
}

/**
 * The preference to show for, and send with, `conversation`'s next turn.
 *
 * - Started (a first prompt is recorded): the server bound the conversation on
 *   that first turn (D6's Conversation affinity) to what was posted then, and
 *   `boundPreference` is that value. A row with no `boundPreference` sent its
 *   first prompt before bta wired the picker into /chat, when ChatPane always
 *   posted 'auto' — so it is bound 'auto' whatever it has stored (a pre-D-9
 *   alias included, which /chat would refuse with a 422 on every turn).
 * - Not started: the stored value only if the SERVED catalog lists it (D-9:
 *   only public ids travel), otherwise the catalog's default. The offline
 *   fallback lists 'auto' alone, so it never lets a stored alias through.
 */
export function preferenceToSend(conversation: Conversation | undefined, catalog: ModelCatalog): string {
  if (conversation === undefined) return catalog.default
  if (conversation.hasFirstPrompt) return conversation.boundPreference ?? AUTO_PREFERENCE
  const served = catalog !== FALLBACK_CATALOG
  if (served && catalog.models.some((m) => m.id === conversation.modelPreference)) {
    return conversation.modelPreference
  }
  return catalog.default
}
