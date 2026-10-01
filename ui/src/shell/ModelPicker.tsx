/**
 * ModelPicker — per-conversation model preference selector (b8o.2).
 *
 * Lives in AppHeader beside the theme control. Editable freely before the
 * first prompt (the conversation's strategy isn't bound server-side yet);
 * after that, changing it starts a NEW conversation instead of mutating the
 * current one — the plan's "Conversation affinity": a started conversation's
 * routing strategy is atomically bound on its first request and a later
 * change would either silently diverge from what the server already
 * committed to, or (with the /chat 409 guard, b8o.2 slice 2-3) simply fail.
 * Disabled with no active conversation — nothing to bind a preference to yet
 * (mirrors ChatPane's own attachment-button gating on conversationId) — and
 * for one the local store does not hold, such as a campaign's GM thread
 * (1kg.2.5 critic 16): a pick there binds nothing; its turns send the default.
 */

import * as React from 'react'
import { useAppNav } from './AppNav'
import { useConversationStore } from './ConversationStoreContext'
import { useModelCatalogState } from './ModelCatalogContext'
import { FALLBACK_CATALOG, preferenceToSend } from './modelPreference'
import type { ModelCatalog } from './modelPreference'
import './ModelPicker.css'

export type { ModelCatalogEntry } from './modelPreference'

export type GetModelsFn = () => Promise<ModelCatalog>

async function defaultGetModels(): Promise<ModelCatalog> {
  const res = await fetch('/models', { credentials: 'include' })
  if (!res.ok) return FALLBACK_CATALOG
  try {
    return (await res.json()) as ModelCatalog
  } catch {
    return FALLBACK_CATALOG
  }
}

export interface ModelPickerProps {
  getModels?: GetModelsFn
  /** Injectable for tests; defaults to the browser's window.confirm. */
  confirmChange?: (message: string) => boolean
}

export function ModelPicker({
  getModels = defaultGetModels,
  confirmChange = (message: string) => window.confirm(message),
}: ModelPickerProps): React.JSX.Element {
  const { mode, conversationId, setConversationId } = useAppNav()
  const store = useConversationStore()
  // agent-forge-harness-bta: the catalog lives where ChatPane reads it too
  // (ModelCatalogProvider), so the pane never sends what this does not show.
  const [catalog, setCatalog] = useModelCatalogState()

  React.useEffect(() => {
    let cancelled = false
    getModels()
      .then((body) => {
        if (!cancelled) setCatalog(body)
      })
      .catch(() => {
        if (!cancelled) setCatalog(FALLBACK_CATALOG)
      })
    return () => {
      cancelled = true
    }
  }, [getModels, setCatalog])

  const conversation = conversationId !== null ? store.get(conversationId) : undefined
  // What this conversation's next turn sends — the same rule ChatPane posts by.
  const value = preferenceToSend(conversation, catalog)
  const stored = conversation?.modelPreference

  // a6o: a stored preference the served catalog does not list (a model alias
  // stored before D-9, or a retired id) goes back to the default, first prompt
  // or not. It can no longer change what a started conversation SENDS — that
  // is its `boundPreference` (bta) — so this only keeps the store honest. Only
  // the served catalog can tell: the offline fallback lists 'auto' alone, and
  // an alias list here would name the models in the bundle.
  React.useEffect(() => {
    if (catalog === FALLBACK_CATALOG || conversationId === null || stored === undefined) return
    if (catalog.models.some((m) => m.id === stored)) return
    store.setModelPreference(conversationId, catalog.default)
  }, [catalog, conversationId, store, stored])

  const handleChange = (next: string): void => {
    if (conversationId === null || conversation === undefined) return
    if (!conversation.hasFirstPrompt) {
      store.setModelPreference(conversationId, next)
      return
    }
    const label = catalog.models.find((m) => m.id === next)?.display_name ?? next
    if (!confirmChange(`Start a new conversation with ${label}?`)) return
    const fresh = store.create(mode, undefined, next)
    setConversationId(fresh.id)
  }

  return (
    <label className="model-picker">
      <span className="model-picker__label">Model</span>
      <select
        className="model-picker__select"
        value={value}
        disabled={conversationId === null || conversation === undefined}
        onChange={(e) => handleChange(e.target.value)}
        aria-label="Model"
      >
        {catalog.models.map((m) => (
          <option key={m.id} value={m.id}>{m.display_name}</option>
        ))}
      </select>
    </label>
  )
}
